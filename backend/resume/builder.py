"""Layer 1: per-job resume TEXT generation. Fast, no PDF.

The profile is evidence. We assemble every FACT (names, titles, dates, locations, tech,
GPA, links) in Python straight from the profile so the model cannot alter ground truth.
The model only does two things: (1) rewrite bullet PROSE into Google XYZ form, slanted to
the job's focus angle, and (2) SELECT/ORDER skills from the profile's own pool. Both are
validated against the profile before they reach the resume.
"""
import re
import json

from .. import config, llm, profile_store, embeddings, timing
from . import evidence, jd_signals, quality, skills_source
# embeddings used for evidence ranking before the LLM; unloaded before chat so models don't thrash.


def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) > 0.5


def _anchor_word(evidence_bullet: str, paren: str) -> str:
    """The word a parenthetical follows in the evidence (e.g. 'plan' in 'floor plan (any …)')."""
    idx = evidence_bullet.find(paren)
    if idx <= 0:
        return ""
    m = re.search(r"([A-Za-z0-9²]+)\s*$", evidence_bullet[:idx].rstrip())
    return m.group(1) if m else ""


def _ensure_specifics(bullets: list[str], evidence: list[str]) -> tuple[list[str], list[str]]:
    """Re-insert a protected parenthetical ONLY when its substance is genuinely absent AND we have a
    confident place to put it. Returns (bullets, warnings).

    History (the fix for the recurring duplicated-phrasing bug): this used an exact-substring
    presence test, so a parenthetical the model had correctly folded into prose looked "dropped" and
    got re-appended verbatim -> "achieving ~95-100% completion (~95-100% completion for the top
    methods)". It also blind-appended to the end of the bullet when the anchor word was missing,
    producing specifics attached to the wrong clause -> "onboarding 100 test users (Auth, Firestore,
    Storage)". Both are now impossible:
      * coverage is token-based (quality.paren_covered), so any wording counts as present;
      * insertion happens only at a matched anchor word — no anchor means we WARN instead of
        corrupting the sentence, because a specific in the wrong clause is worse than a missing one.
    """
    protected = _parentheticals(evidence)
    warnings: list[str] = []
    if not protected or not bullets:
        return bullets, warnings
    bullets = list(bullets)
    for paren in protected:
        joined = " ".join(bullets)
        # Already stated anywhere (in any wording, or subsumed by a longer parenthetical)? Done.
        if quality.paren_covered(paren, joined):
            continue
        src = next((e for e in evidence if paren in e), "")
        ti = max(range(len(bullets)), key=lambda i: len(_words(bullets[i]) & _words(src)))
        b, anchor = bullets[ti], _anchor_word(src, paren)
        m = re.search(r"\b" + re.escape(anchor) + r"s?\b", b, re.IGNORECASE) if anchor else None
        if not m:
            warnings.append(f"specific {paren} was dropped by the rewrite and had no anchor "
                            f"('{anchor or '?'}') in the bullet — left out rather than misplaced")
            continue
        bullets[ti] = b[:m.end()] + " " + paren + b[m.end():]
    return bullets, warnings


# A sentence-final clause carrying a hard number, e.g.
#   ", cutting about 10 minutes of manual form-filling from every application."
# Restricted to a GERUND opener on purpose: a participial phrase attaches cleanly to the end of any
# finished clause, so re-appending one can't produce the mangled grammar that blind insertion does.
_METRIC_CLAUSE = re.compile(
    r",\s*((?:[a-z]+ing)\b[^,.]*?\b\d[\d,.]*\s*"
    r"(?:%|percent|x|ms|sec|seconds?|mins?|minutes?|hours?|days?|weeks?|months?|users?|"
    r"customers?|requests?|records?|rows?|lines?|students?|people)\b[^.]*)\.?\s*$", re.I)
_NUM_ONLY = re.compile(r"\b\d[\d,.]*\b")
_UNIT_NUM = re.compile(r"\b(\d[\d,.]*)\s*"
                       r"(%|percent|x|ms|sec|seconds?|mins?|minutes?|hours?|days?|weeks?|months?|"
                       r"users?|customers?|requests?|records?|rows?|lines?|students?|people)\b", re.I)


def _ensure_metric(bullets: list[str], evidence: list[str],
                   declared: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Put back a hard metric the rewrite dropped. Returns (bullets, warnings).

    Same reasoning as _ensure_specifics, for the one kind of specific that matters most on a resume.
    The prompt already tells the model to keep real numbers and it mostly does — but "mostly" is not
    good enough for the single quantified claim on an entry, and a local model condensing a long
    evidence bullet drops the trailing result clause first. Python owns the facts here, so Python
    puts it back rather than hoping the next generation behaves.

    Deliberately conservative: only a gerund clause, only appended to a bullet that already talks
    about the same thing, and only when that number is absent from EVERY bullet in the entry.
    """
    warnings: list[str] = []
    if not bullets or not evidence:
        return bullets, warnings
    joined = " ".join(bullets)
    # Coverage is tested on the BARE NUMBER, not on number+unit adjacency. The model rephrases
    # freely — "helping 160 students" comes back as "Supported 160 introductory Python students",
    # where the unit no longer sits next to the digits. An adjacency test reads that as dropped and
    # appends the clause again, giving "Supported 160 ... helping 160 students". A false positive
    # here costs nothing (we simply don't re-add); a false negative duplicates the metric.
    have = {n.replace(",", "").rstrip(".") for n in _NUM_ONLY.findall(joined)}
    bullets = list(bullets)
    for ev in evidence:
        m = _METRIC_CLAUSE.search(ev.strip())
        if not m:
            continue
        clause = m.group(1).strip()
        # _UNIT_NUM picks out which number in the clause is the METRIC (it has a unit attached);
        # the comparison against `have` is then done on that number alone.
        nums = {n.replace(",", "").rstrip(".") for n, _u in _UNIT_NUM.findall(clause)}
        if not nums or nums & have:
            continue                       # the model kept it (in some wording) — leave it alone
        # Attach to whichever bullet is already about this evidence, so the claim lands on the
        # work that earned it rather than on whatever happens to be last.
        ti = max(range(len(bullets)), key=lambda i: len(_words(bullets[i]) & _words(ev)))
        base = bullets[ti].rstrip().rstrip(".")
        if len(re.findall(r"\w+", base + " " + clause)) > 34:
            warnings.append(f"metric '{clause[:40]}…' left out — the bullet it belongs to is "
                            f"already at the length limit")
            continue
        bullets[ti] = f"{base}, {clause}."
        have |= nums

    # Declared metrics (the profile's `metrics` field). Same coverage rule — if the number is
    # anywhere in this entry's bullets the model already worked it in, in whatever wording.
    for metric in (declared or []):
        nums = {n.replace(",", "").rstrip(".") for n in _NUM_ONLY.findall(metric)}
        if not nums:
            warnings.append(f"metric '{metric[:40]}' has no number in it — nothing to enforce")
            continue
        if nums & have:
            continue
        ti = max(range(len(bullets)), key=lambda i: len(_words(bullets[i]) & _words(metric)))
        base = bullets[ti].rstrip().rstrip(".")
        # A participial phrase trails a finished clause cleanly; anything else is parenthesised,
        # which is grammatical whatever the user typed.
        tail = (f", {metric}" if re.match(r"[a-z]+ing\b", metric.strip(), re.I)
                else f" ({metric})")
        if len(re.findall(r"\w+", base + tail)) > 34:
            warnings.append(f"metric '{metric[:40]}' left out — its bullet is already at the "
                            f"length limit")
            continue
        bullets[ti] = base + tail + "."
        have |= nums
    return bullets, warnings


def _dedupe_against_sources(bullets: list[str], sources: list[str]) -> tuple[list[str], list[dict]]:
    """Make every bullet in an entry cover a DIFFERENT source fact.

    The model routinely rewrites one evidence bullet twice and drops another entirely — e.g. two
    bullets both describing "React Native app on Firebase delivering real-time multi-floor
    navigation" while the founding/contract fact disappears. Nothing upstream noticed, because each
    bullet is individually valid and they share few literal words.

    Repair: assign each bullet to its nearest source fact; where two bullets claim the same source,
    replace the weaker one with a source fact nothing covered yet (used verbatim — it is the user's
    own honest wording). If every source is already covered, drop the redundant bullet rather than
    restate. Returns (bullets, notes).

    Uses token overlap only (no embeddings). Embedding-based matching here reloaded nomic AFTER the
    resume LLM and cost several seconds of thrash into the cover-letter step — token Jaccard is
    good enough for "same source fact" on evidence this short.
    """
    notes: list[dict] = []
    real_ev = [e for e in (sources or []) if (e or "").strip()]
    if len(bullets) < 2 or not real_ev:
        return bullets, notes

    token_pairs = quality.redundant_pairs(bullets)
    flagged = {p["redundant"] for p in token_pairs}
    if not flagged:
        return bullets, notes

    # Which source facts are already spoken for, and by whom (token Jaccard).
    owner: dict[int, int] = {}
    for i, b in enumerate(bullets):
        wb = _words(b)
        if not wb:
            continue
        best, best_sim = -1, -1.0
        for si, src in enumerate(real_ev):
            ws = _words(src)
            if not ws:
                continue
            sim = len(wb & ws) / len(wb | ws)
            if sim > best_sim:
                best, best_sim = si, sim
        if best >= 0 and best_sim >= 0.35:
            owner.setdefault(best, i)
    uncovered = [i for i in range(len(real_ev)) if i not in owner]

    out = list(bullets)
    for idx in sorted(flagged):
        if uncovered:
            src = uncovered.pop(0)
            notes.append({"replaced_bullet": out[idx][:70], "reason": "restated an earlier bullet",
                          "used_source": real_ev[src][:70]})
            out[idx] = real_ev[src]
        else:
            notes.append({"dropped_bullet": out[idx][:70],
                          "reason": "restated an earlier bullet and every source fact was covered"})
            out[idx] = ""
    return [b for b in out if b.strip()], notes


def _topup(kept: list[str], evidence: list[str], target: int) -> list[str]:
    """Bring a short bullet list up to `target` by adding evidence bullets the model didn't
    already cover (skipping near-duplicates of what's already there)."""
    out = list(kept)
    for ev in evidence:
        if len(out) >= target:
            break
        if not any(_similar(ev, b) for b in out):
            out.append(ev)
    for ev in evidence:  # if still short (all looked similar), pad with whatever's left
        if len(out) >= target:
            break
        if ev not in out:
            out.append(ev)
    return out[:target]

_SYS = (
    "You are a brutal technical resume editor for CS new-grad/early-career roles. You make "
    "every bullet shorter, more specific, and more honest. You never invent numbers, metrics, or "
    "outcomes, and you never pad with filler. You rephrase ONLY the candidate's own evidence. "
    "You NEVER introduce a technology, language, or tool that isn't already in that candidate's "
    "evidence, no matter what the target job asks for. Return strict JSON only."
)

_BULLET_RULE = (
    "Write each bullet as ONE tight sentence that answers the implicit \"So what?\": a strong "
    "past-tense action verb + the specific accomplishment and its technical challenge + the IMPACT "
    "(what it ENABLED, the problem it SOLVED, or how it helped users / reliability / automation / "
    "scale). Lead with the result. Aim for ~12-24 words — a recruiter skims it in seconds.\n"
    "OUTCOME OVER IMPLEMENTATION: don't just say what was built — make the value obvious. Where a "
    "bullet would become a list of technologies, say what those technologies ENABLED instead of "
    "just naming them (not 'built with Firebase Auth, Firestore, Storage' but '…on Firebase, "
    "enabling real-time multi-floor sync for students'). Keep technical depth; cut documentation-"
    "style feature lists.\n"
    "KEEP SPECIFICS: preserve EVERY concrete detail from the evidence — exact numbers, named "
    "examples (e.g. a parenthetical list of scenarios), file formats, and specific tech names. "
    "These ARE the substance; never drop them to hit a word count. Trim formula and filler, "
    "not facts. If the evidence lists examples like '(medical emergencies, market shocks, "
    "emergency spending)', keep that list.\n"
    "METRICS: include a number ONLY if a REAL hard number is in the evidence (users, ms, R^2, "
    "%, requests/sec, a count, TIME SAVED such as minutes or hours per task). A time-saved figure is a hard number and must be kept. Rewrite the bullet cleanly AROUND that number. If there is no real "
    "number, land the bullet on the concrete CAPABILITY it unlocked instead — NEVER invent a "
    "number, user count, percentage, latency, or business metric.\n"
    "INFERRED IMPACT (allowed, but only when DIRECT): you MAY state a qualitative outcome that is a "
    "direct, reasonable consequence of the work — e.g. 'enabling offline multi-floor routing', 'so "
    "users publish maps with zero manual graph work', 'removing a manual mapping step'. Never a "
    "consequence that isn't clearly implied by the evidence, and never a fabricated quantity.\n"
    "BANNED PHRASES — never write 'as measured by', 'as evidenced by', 'as proven by', 'as "
    "indicated by'. BANNED CIRCULAR/VAGUE OUTCOMES — never 'high accuracy', 'positive "
    "feedback', 'improved performance', 'scalable deployment', 'successful completion', "
    "'enhanced user experience', or any outcome that just RESTATES the action. A valid outcome is a "
    "real NUMBER, a concrete external event (shipped, deployed, contracted, accepted, launched), OR "
    "a specific capability the work directly enabled (offline routing, zero manual setup, real-time "
    "sync).\n"
    "NO FILLER: cut anything true of any developer (e.g. 'used Git for version control'). "
    "Every bullet must be specific to THIS project/role.\n"
    "No first-person pronouns. NEVER invent numbers, metrics, or outcomes not in the evidence. "
    "NEVER introduce a technology, language, or tool that is not already named in this entry's "
    "evidence bullets or tech list — not even one the target job asks for."
)


def _parentheticals(bullets: list[str]) -> list[str]:
    """Protected specifics: parenthetical lists/values in the evidence (e.g.
    '(medical emergencies, market shocks, emergency spending)', '(R^2 > 0.8)', '(any image
    file)'). The rewriter must keep these verbatim — they're the substance."""
    out, seen = [], set()
    for b in bullets or []:
        for m in re.findall(r"\([^)]{2,}\)", b or ""):
            if m.lower() not in seen:
                seen.add(m.lower())
                out.append(m)
    return out


def _target_bullets(entry: dict, default_max: int) -> int:
    """How many bullets this entry should get: its profile max_bullets, capped by how much
    evidence it actually has. This is the per-entry count the user sets on the website."""
    evidence = len([b for b in (entry.get("bullets") or []) if b.strip()])
    return max(1, min(int(entry.get("max_bullets", default_max) or default_max), evidence))


def _select_evidence(entry: dict, default_max: int, jd_emb, bull_embs):
    """The strongest, most JD-relevant evidence to actually send the model (a leaner, cleaner
    prompt), always keeping bullets that carry a protected parenthetical. Returns (sel, must, target)."""
    all_ev = [b for b in (entry.get("bullets") or []) if b.strip()]
    target = _target_bullets(entry, default_max)
    must = _parentheticals(entry.get("bullets"))
    protected = [b for b in all_ev if any(p in b for p in must)]
    keep = min(len(all_ev), target + 2)        # a little headroom for the model to choose from
    sel = evidence.select(all_ev, jd_emb, bull_embs, keep=keep, protected=protected)
    return sel, must, target


def _top_project_ids(projects: list[dict], jd_emb, bull_embs, keep: int = 2) -> set[str]:
    """The resume shows only the `keep` projects most relevant to THIS job. Score each project by
    the best JD-relevance among its bullets and keep the top ones (ties fall back to list order)."""
    if len(projects) <= keep:
        return {p["id"] for p in projects}
    scored = []
    for i, p in enumerate(projects):
        rels = [embeddings.cosine(jd_emb, bull_embs.get(b) or [])
                for b in (p.get("bullets") or []) if b.strip() and bull_embs.get(b)]
        scored.append((max(rels) if rels else 0.0, -i, p["id"]))
    scored.sort(reverse=True)
    return {pid for _, _, pid in scored[:keep]}


def _evidence_for_entries(entries: list[dict], kind: str, jd_emb, bull_embs) -> list[dict]:
    """Lean payload for the rewriter. Empty optional fields are omitted — they still cost tokens
    (and prompt-eval time) when sent as \"\" / []. Same evidence, fewer wasted tokens."""
    out = []
    for e in entries:
        if kind == "work":
            sel, must, target = _select_evidence(e, 4, jd_emb, bull_embs)
            row = {
                "id": e["id"],
                "role": f'{e.get("title","")} at {e.get("company","")}',
                "evidence_bullets": sel,
                "tech": _flatten(e.get("tech_used")),
                "target_bullets": target,
            }
            ctx = (e.get("role_summary") or "").strip()
            if ctx:
                row["context"] = ctx
            metrics = _metrics_of(e)
            if metrics:
                row["metrics"] = metrics
            if must:
                row["must_include"] = must
            out.append(row)
        else:  # project
            sel, must, target = _select_evidence(e, 3, jd_emb, bull_embs)
            row = {
                "id": e["id"],
                "name": e.get("name", ""),
                "evidence_bullets": sel,
                "tech": _flatten(e.get("tech_stack")),
                "target_bullets": target,
            }
            ctx = (e.get("context") or "").strip()
            if ctx:
                row["context"] = ctx
            metrics = _metrics_of(e)
            if metrics:
                row["metrics"] = metrics
            if must:
                row["must_include"] = must
            out.append(row)
    return out


def _metrics_of(entry: dict) -> list[str]:
    """The entry's declared hard numbers. Structured rather than buried in bullet prose, because a
    metric is a FACT — the same category as a date or an employer — and facts are Python's job here,
    not something to hope survives a rewrite."""
    return [m.strip() for m in (entry.get("metrics") or []) if isinstance(m, str) and m.strip()]


def _flatten(val) -> str:
    if isinstance(val, list):
        return ", ".join(x for x in val if isinstance(x, str) and x.strip())
    return val or ""


# Does this job actually involve LLMs? (Only then may the AI-tool exception below apply.)
_LLM_JD_RE = re.compile(
    r"\b(llms?|large language models?|generative ai|gen ?ai|genai|prompt(?:ing|s)?|openai|gpt|"
    r"claude|anthropic|gemini|language models?|ai agents?|agentic|rag|embeddings?|"
    r"fine[- ]?tun\w*|chatbots?|copilot)\b", re.I)


def jd_is_llm(jd_analysis: dict) -> bool:
    blob = " ".join([
        jd_analysis.get("summary", ""), jd_analysis.get("role_title", ""),
        " ".join(jd_analysis.get("responsibilities") or []),
        " ".join(jd_analysis.get("concrete_tech") or []),
        jd_analysis.get("jd_text", ""),
    ])
    return bool(_LLM_JD_RE.search(blob))


def llm_tools(profile: dict) -> list[str]:
    """AI/LLM tools the candidate genuinely uses to build their software (skills.ai_tools, plus
    Claude/Claude API). Allowed into a fitting software bullet ONLY when the job involves LLMs."""
    tools = list((profile.get("skills", {}) or {}).get("ai_tools", []) or [])
    for extra in ("Claude", "Claude API"):
        if not any(extra.lower() == t.lower() for t in tools):
            tools.append(extra)
    return tools


def resume_work(profile: dict) -> list[dict]:
    """Work entries that belong on the RESUME. An entry with "on_resume": false stays in the
    profile as real employment history (it still answers "list your jobs" application questions)
    but is kept off the one-page resume — the page only has room for the roles that argue for the
    job. Missing flag = shown, so every existing entry keeps its behavior."""
    return [e for e in (profile.get("work_experience") or []) if e.get("on_resume") is not False]


def generate_text(jd_analysis: dict, focus_angle: str, feedback: str | None = None) -> dict:
    """Produce the reviewable text layer for the whole resume in a single LLM call."""
    import time as _time
    timing.reset("resume")
    t_all = _time.time()
    profile = profile_store.load()
    work = resume_work(profile)
    projects = profile.get("projects", []) or []

    jd_keywords = jd_analysis.get("concrete_tech", []) or []
    # AI/LLM tools are fair game ONLY when the job itself is about LLMs (the candidate really used
    # them to build their software projects, so surfacing them then is honest, not fabrication).
    llm_allowed = llm_tools(profile) if jd_is_llm(jd_analysis) else []

    # Deterministic JD structure + semantic evidence ranking (Python does the thinking, not the LLM).
    # Embeddings are cached per bullet, so this adds only ONE embed call (the JD) per generation.
    with timing.stage("evidence_embed"):
        jd_emphasis = jd_signals.categorize(jd_analysis)["categories"]
        jd_emb = evidence.jd_embedding(jd_signals.jd_query(jd_analysis, focus_angle))
        all_ev = ([b for e in work for b in (e.get("bullets") or []) if b.strip()]
                  + [b for p in projects for b in (p.get("bullets") or []) if b.strip()])
        bull_embs = evidence.embed_bullets(all_ev)

        # Only the two projects most relevant to THIS job make the resume (user keeps 3+ on file).
        keep_ids = _top_project_ids(projects, jd_emb, bull_embs, keep=2)
        projects = [p for p in projects if p["id"] in keep_ids]

    # NOTE: the global skill pool is deliberately NOT passed to the rewrite — the model may only
    # use each entry's OWN "tech" allow-list (below), never a skill borrowed from elsewhere.
    # Compact JSON (no indent/spaces) — same facts, fewer prompt tokens → faster prompt-eval.
    job = {
        "role_title": jd_analysis.get("role_title", ""),
        "company": jd_analysis.get("company", ""),
        "summary": jd_analysis.get("summary", ""),
        "keywords": jd_keywords,
        "responsibilities": (jd_analysis.get("responsibilities") or [])[:6],
    }
    payload = {
        "focus_angle": focus_angle,
        "job": job,
        "jd_emphasis": jd_emphasis,   # deterministic category signals — for EMPHASIS, not new facts
        "work_experience": _evidence_for_entries(work, "work", jd_emb, bull_embs),
        "projects": _evidence_for_entries(projects, "project", jd_emb, bull_embs),
    }
    if llm_allowed:
        payload["llm_tools_allowed"] = llm_allowed
    if (profile.get("ai_preferences") or {}).get("voice") == "no-pronoun":
        payload["no_pronoun"] = True
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    # Only include rules that apply to THIS call. Empty llm_tools_allowed used to still spend
    # ~350 tokens explaining an exception that does not fire — that is pure prompt-eval cost.
    llm_rule = ""
    if llm_allowed:
        llm_rule = """
- LLM-TOOLS EXCEPTION: "llm_tools_allowed" lists AI/LLM tools the candidate really uses. Work
  exactly ONE into the single best-fitting SOFTWARE project/work bullet (honest, natural). Never
  put it in non-software work. Never fabricate an outcome to justify it."""

    instr = f"""Rewrite this candidate's resume text for the target job, slanted to the focus
angle "{focus_angle}".

{_BULLET_RULE}

Rules:
- Return rewritten bullets for EVERY work_experience and project entry, keyed by its "id".
- Produce EXACTLY each entry's "target_bullets" count — no fewer, no more.
- PER-ENTRY ALLOWED SKILLS: each entry's "tech" list + its evidence_bullets are the ONLY
  technologies allowed in that entry. Surface them where they fit; never borrow from another
  entry; never add a tech just because "keywords" mention it (keywords are emphasis only).
  When in doubt, leave it out.{llm_rule}
- "metrics" (when present): work EVERY listed number into that entry exactly once, on the
  bullet that produced it. Never invent a number.
- "must_include" (when present): every string MUST appear VERBATIM. Write around it — do not
  also restate its numbers/words in the same bullet's main clause.
- NICHE tools: if uncommon, show HOW it was used in a few words. Mainstream (Python, React,
  Git, common DBs) need no explanation.
- EMPHASIS: slant toward "jd_emphasis" categories that genuinely overlap the evidence —
  word choice only; no new facts.
- NO SELF-GRADING on tools the candidate built: describe what it DOES, never how WELL
  ("without fabricating", "accurately", "reliably", "guarantees", …).
- BANNED FILLER: leveraged, utilized, worked on, responsible for, participated in, helped,
  assisted with, successfully, various, multiple, several.
- VERB VARIETY: do not start more than one bullet with the same verb across the resume.

Return JSON:
{{
  "work": {{ "<id>": ["bullet", ...], ... }},
  "projects": {{ "<id>": ["bullet", ...], ... }}
}}

Input:
{payload_json}"""

    if feedback:
        instr += f"\n\nThe user reviewed a previous draft and gave this feedback — apply it:\n{feedback}"

    # Drop nomic before the chat model runs — both co-resident is what slows prompt eval ~10–15s.
    embeddings.unload()
    with timing.stage("resume_llm"):
        result = llm.chat_json(_SYS, instr, temperature=0.5 if not feedback else 0.4,
                               num_predict=config.LLM_PREDICT_RESUME)
    with timing.stage("resume_assemble"):
        assembled = _assemble(profile, result, jd_analysis, focus_angle, keep_project_ids=keep_ids)
        # The local model reliably ignores the LLM-tools instruction, so guarantee it here for LLM
        # jobs: surface Claude in one project bullet (the candidate really builds with it — honest).
        assembled = _surface_llm_tool(assembled, llm_allowed)
        # Deterministic polish (no LLM): strip filler, dedupe opening verbs, order strongest-first,
        # then attach ATS + validation diagnostics. This is where "Python gets smarter" happens.
        out = _polish(assembled, jd_analysis, profile)
    timing.log_summary(_time.time() - t_all)
    return out


_LLM_MENTION_RE = re.compile(
    r"\b(claude|gpt|llm|openai|gemini|chatgpt|cursor|codex|copilot|language model)\b", re.I)


def _surface_llm_tool(assembled: dict, llm_allowed: list[str]) -> dict:
    """For an LLM job, ensure ONE project bullet names the candidate's real LLM tooling (Claude).
    Deterministic (the model won't do it), idempotent, and only touches a software project."""
    if not llm_allowed:
        return assembled
    projects = assembled.get("projects", [])
    if not projects:
        return assembled
    # already surfaced somewhere in the projects? then leave it alone
    if any(_LLM_MENTION_RE.search(b) for p in projects for b in p.get("bullets", [])):
        return assembled
    # attach to the first project's first bullet, before its terminal period, kept short + honest
    p = projects[0]
    if not p.get("bullets"):
        return assembled
    b = p["bullets"][0].rstrip()
    clause = "using Claude and LLM APIs to accelerate development"
    p["bullets"][0] = (b[:-1] if b.endswith(".") else b) + f", {clause}."
    return assembled


def _allow_set(val) -> set[str]:
    items = val if isinstance(val, list) else ([val] if val else [])
    out = set()
    for s in items:
        for part in re.split(r"[,/]", s or ""):
            if part.strip():
                out.add(part.strip().lower())
    return out


def _entry_facts(profile: dict) -> dict:
    """id -> {tech: allow-set, evidence: joined raw bullets} for deterministic validation."""
    facts = {}
    for e in (profile.get("work_experience") or []):
        facts[e["id"]] = {"tech": _allow_set(e.get("tech_used")),
                          "evidence": " ".join(b for b in (e.get("bullets") or []) if b.strip())}
    for p in (profile.get("projects") or []):
        facts[p["id"]] = {"tech": _allow_set(p.get("tech_stack")),
                          "evidence": " ".join(b for b in (p.get("bullets") or []) if b.strip())}
    return facts


def _polish(gen: dict, jd_analysis: dict, profile: dict) -> dict:
    """Deterministic post-rewrite pass (NO LLM). Strip filler, order strongest-first, dedupe opening
    verbs across the whole resume, then attach ATS + validation diagnostics."""
    jterms = jd_signals.jd_terms(jd_analysis)

    # 1) clean generic filler, strip self-duplication, order strongest-first (impact + JD overlap)
    # The duplication strip is a BACKSTOP: _ensure_specifics no longer creates duplicates, but this
    # catches the same defect arriving from anywhere else (the model restating a parenthetical in its
    # own clause, a stray unbalanced paren) before anything reaches the PDF.
    dup_removed, claims_removed = [], []
    for section in ("work", "projects"):
        for e in gen.get(section, []) or []:
            cleaned = []
            for b in (e.get("bullets") or []):
                b = quality.clean_generic(b)[0]
                b, removed = quality.strip_internal_duplication(b)
                if removed:
                    dup_removed.append({"entry": e.get("id"), "removed": removed, "bullet": b[:70]})
                # Unproven claims about THIS tool's own correctness are stripped, not just flagged —
                # an unverifiable "without fabricating skills" on a resume is the exact failure the
                # pipeline is supposed to prevent, so it must never reach the PDF.
                b, claims = quality.strip_self_capability_claims(b)
                if claims:
                    claims_removed.append({"entry": e.get("id"), "removed": claims,
                                           "bullet": b[:70]})
                cleaned.append(b)
            e["bullets"] = quality.order_bullets(cleaned, jterms)

    # 2) global opening-verb dedupe (resume reading order: work, then projects)
    spans, flat = [], []
    for section in ("work", "projects"):
        for e in gen.get(section, []) or []:
            spans.append(e)
            flat += e.get("bullets") or []
    deduped, _ = quality.dedupe_opening_verbs(flat)
    i = 0
    for e in spans:
        n = len(e.get("bullets") or [])
        e["bullets"] = deduped[i:i + n]
        i += n

    # 3) deterministic validation (informational — surfaced, not auto-regenerated)
    facts = _entry_facts(profile)
    validation = []
    for section in ("work", "projects"):
        for e in gen.get(section, []) or []:
            f = facts.get(e.get("id"), {})
            for b in (e.get("bullets") or []):
                issues = quality.validate_bullet(b, f.get("tech", set()), f.get("evidence", ""))
                issues += quality.internal_duplication(b)      # anything the strip couldn't fix
                if issues:
                    validation.append({"entry": e.get("id"), "bullet": b[:70], "issues": issues})
            # Cross-bullet redundancy that survived the repair in _assemble (e.g. every source fact
            # was already covered, so there was nothing distinct to swap in). Surfaced, not silent.
            bl = e.get("bullets") or []
            for pair in quality.redundant_pairs(bl):
                validation.append({
                    "entry": e.get("id"), "bullet": bl[pair["redundant"]][:70],
                    "issues": [f"restates bullet {pair['keep'] + 1} "
                               f"(overlap {pair['overlap']}, shared: {', '.join(pair['shared'][:5])})"]})
            # Semantic (embedding) redundancy is checked in _assemble via _dedupe_against_sources.
            # Re-running it here reloads nomic after the resume LLM and costs ~1–2s + thrash into
            # the cover-letter step — skip the duplicate pass.
            for i in quality.bullets_adding_nothing(bl):
                if i:      # bullet 1 has nothing prior to add to
                    validation.append({"entry": e.get("id"), "bullet": bl[i][:70],
                                       "issues": ["adds no new tech/metric/feature over earlier bullets"]})

    gen["ats"] = quality.ats_report(gen, jd_analysis)
    gen["validation"] = validation
    gen["duplication_removed"] = dup_removed
    # Skills honesty, surfaced at generation time (BUG 2): a verified skill with no recorded
    # evidence, or whose evidence isn't among the experiences this resume actually shows.
    # `skills_unverified` must always be empty — non-empty means something bypassed skills_source.
    gen["skills_warnings"] = skills_source.evidence_warnings(gen)
    gen["skills_unverified"] = skills_source.unknown_skill_report(gen)
    gen["capability_claims_removed"] = claims_removed
    # Root-cause check: an unverifiable self-claim in the PROFILE evidence makes the model
    # regenerate that claim every run, so stripping the output only ever treats the symptom.
    # Surfaced against the profile so it can be fixed at source.
    gen["source_evidence_warnings"] = _source_claim_warnings(profile)
    return gen


def _source_claim_warnings(profile: dict) -> list[dict]:
    """Self-capability claims sitting in the user's own profile bullets. These are the upstream
    reason such claims keep reappearing in generated resumes."""
    out = []
    for key, label in (("work_experience", "company"), ("projects", "name")):
        entries = resume_work(profile) if key == "work_experience" else (profile.get(key) or [])
        for e in entries:
            for b in (e.get("bullets") or []):
                claims = quality.self_capability_claims(b)
                if claims:
                    out.append({"entry": e.get(label, ""), "bullet": b[:90], "claims": claims,
                                "detail": "unverifiable claim in your profile evidence — the "
                                          "rewriter keeps reproducing it; edit the profile bullet"})
    return out


def _assemble(profile: dict, model: dict, jd_analysis: dict, focus_angle: str,
              keep_project_ids: set[str] | None = None) -> dict:
    """Combine model prose with profile FACTS. Facts always win."""
    specifics_warnings: list[dict] = []
    redundancy_notes: list[dict] = []
    work_out = []
    for e in resume_work(profile):
        evid = [b for b in (e.get("bullets") or []) if b.strip()]
        target = _target_bullets(e, 4)
        bullets = [b for b in (model.get("work", {}).get(e["id"]) or [])
                   if isinstance(b, str) and b.strip()][:target]
        if len(bullets) < target:  # model under-delivered — top up from real evidence bullets
            bullets = _topup(bullets, evid, target)
        # Each bullet must advance to a NEW source fact before specifics are re-checked, so a
        # replacement bullet gets its own parenthetical handling rather than inheriting the
        # duplicate's.
        bullets, notes = _dedupe_against_sources(bullets, evid)
        redundancy_notes += [{"entry": e["id"], **n} for n in notes]
        bullets, warns = _ensure_specifics(bullets, evid)
        specifics_warnings += [{"entry": e["id"], "warning": w} for w in warns]
        bullets, warns = _ensure_metric(bullets, evid, _metrics_of(e))
        specifics_warnings += [{"entry": e["id"], "warning": w} for w in warns]
        if not bullets:
            continue
        work_out.append({
            "id": e["id"],
            "company": e.get("company", ""),
            "title": e.get("title", ""),
            "location": e.get("location", ""),
            "dates": _date_range(e.get("start_date"), e.get("end_date"), e.get("current")),
            "bullets": bullets,
        })

    proj_out = []
    for p in profile.get("projects", []) or []:
        if keep_project_ids is not None and p["id"] not in keep_project_ids:
            continue   # only the JD-selected projects (max 2) render
        evid = [b for b in (p.get("bullets") or []) if b.strip()]
        target = _target_bullets(p, 3)
        bullets = [b for b in (model.get("projects", {}).get(p["id"]) or [])
                   if isinstance(b, str) and b.strip()][:target]
        if len(bullets) < target:
            bullets = _topup(bullets, evid, target)
        bullets, notes = _dedupe_against_sources(bullets, evid)
        redundancy_notes += [{"entry": p["id"], **n} for n in notes]
        bullets, warns = _ensure_specifics(bullets, evid)
        specifics_warnings += [{"entry": p["id"], "warning": w} for w in warns]
        bullets, warns = _ensure_metric(bullets, evid, _metrics_of(p))
        specifics_warnings += [{"entry": p["id"], "warning": w} for w in warns]
        if not bullets:
            continue
        proj_out.append({
            "id": p["id"],
            "name": p.get("name", ""),
            "link": p.get("link", ""),
            "dates": p.get("date_range", ""),
            "tech": _flatten(p.get("tech_stack")),
            "bullets": bullets,
        })

    skills_sel = _select_skills(profile, jd_analysis.get("concrete_tech", []) or [])

    return {
        "summary": "",  # no summary line on the resume (user preference)
        "identity": profile.get("identity", {}),
        "education": _education(profile),
        "work": work_out,
        "projects": proj_out,
        "skills": skills_sel,
        "specifics_warnings": specifics_warnings,
        "redundancy_repairs": redundancy_notes,
        "meta": {
            "focus_angle": focus_angle,
            "company": jd_analysis.get("company", ""),
            "role_title": jd_analysis.get("role_title", ""),
        },
    }


def _select_skills(profile: dict, jd_keywords: list[str]) -> dict:
    """The Skills section, sourced EXCLUSIVELY from the hand-maintained verified-skills file.

    Removed 2026-08-17, deliberately, because each was a way for an unverifiable skill to reach the
    resume and made the section differ between runs of the same job:
      * the `skills_extra` pool, promoted whenever the JD happened to name one of its entries
        (this is where Excel came from);
      * `_SKILL_FAMILIES` / `_close_additions`, which added a JD skill the candidate did NOT have
        if they had a "family" sibling — having React added Angular, having SQLite added Oracle;
      * the curated `profile["skills"]` bucket itself, which had accumulated entries with no
        supporting evidence anywhere in the profile.

    The job description may now only influence ORDER (see skills_source.ordered_groups).
    """
    return skills_source.ordered_groups(jd_keywords)


def _education(profile: dict) -> list[dict]:
    out = []
    for ed in profile.get("education", []) or []:
        deg = " ".join(x for x in [ed.get("degree", ""), ed.get("major", "")] if x)
        if ed.get("second_major"):
            deg += f" & {ed['second_major']}"
        gpa = ""
        if ed.get("gpa") and ed.get("show_gpa", True):
            gpa = f"GPA: {ed['gpa']}"
        out.append({
            "school": ed.get("school", ""),
            "location": ed.get("location", ""),
            "degree_line": deg,
            "gpa": gpa,
            "coursework": [c for c in (ed.get("coursework") or []) if c.strip()],
            # NOTE: graduation date intentionally omitted from rendered education (per user).
        })
    return out


def _date_range(start, end, current) -> str:
    start = (start or "").strip()
    end = "Present" if current else (end or "").strip()
    return " – ".join(x for x in [start, end] if x)
