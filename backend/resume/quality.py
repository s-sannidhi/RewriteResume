"""Deterministic resume-quality toolkit — NO LLM. Everything here is regex/heuristic and runs in
microseconds, so it never moves the generation-time needle. Powers:
  • impact_score        (#2)  rank bullets by concrete strength
  • dedupe_opening_verbs(#5)  kill "Implemented… Implemented… Implemented…" with honest synonyms
  • clean_generic       (#10) swap/strip filler ("utilized"->"used", drop "successfully")
  • weakness_reasons    (#6)  flag "worked on / helped / responsible for" etc.
  • validate_bullet     (#13) invented tech/number, banned phrases, length
  • ats_report          (#8)  coverage / verb diversity / readability / relevance estimate
"""
import re
from . import jd_signals

# ---------------------------------------------------------------- impact scoring (#2) ----
_IMPACT = [
    (re.compile(r"\b\d[\d,\.]*\s*(%|percent|x\b|ms\b|sec|seconds?|users?|customers?|requests?|"
                r"rps|qps|mins?|minutes?|hours?|days?|weeks?|months?|lines?|records?|rows?|students?|people)\b"
                r"|\b\d{2,}\b|\br\^?2\b|\bo\(", re.I), 3.0, "metric"),
    (re.compile(r"\b(deployed|shipped|launched|released|in production|production|live)\b", re.I), 1.5, "production"),
    (re.compile(r"\b(scalable|high[- ]throughput|low[- ]latency|real[- ]time|concurrent|distributed|"
                r"fault[- ]tolerant|optimiz)\w*", re.I), 1.2, "complexity"),
    (re.compile(r"\b(algorithm|model|regression|classifier|neural|data structure|complexity|heuristic)\w*|o\(", re.I), 1.0, "algorithm"),
    (re.compile(r"\b(api|apis|endpoint|database|pipeline|microservice|cache|queue|server|backend|infrastructure)\w*", re.I), 1.0, "infra"),
    (re.compile(r"\b(revenue|cost|reduc|increas|sav(ed|ing|es)|growth|adopt|engagement|retention|conversion)\w*", re.I), 1.5, "business"),
    (re.compile(r"\b(led|mentored?|managed|owned|spearheaded|coordinated|drove)\b", re.I), 1.0, "leadership"),
    (re.compile(r"\b(customer|client|user[- ]facing|ux|frontend)\w*", re.I), 0.5, "customer"),
]


def impact_score(text: str) -> float:
    t = text or ""
    return round(sum(w for rx, w, _ in _IMPACT if rx.search(t)), 3)


def impact_signals(text: str) -> list[str]:
    t = text or ""
    return [name for rx, _, name in _IMPACT if rx.search(t)]


# ---------------------------------------------------------------- verb variety (#5) ----
# Meaning-preserving past-tense alternatives, grouped by rough sense. Only the OPENING verb is
# swapped, and the object carries the meaning, so these stay honest.
VERB_SYNONYMS = {
    "built": ["engineered", "developed", "created", "constructed"],
    "implemented": ["built", "engineered", "developed", "added", "created"],
    "developed": ["built", "engineered", "created", "designed"],
    "created": ["built", "developed", "produced", "designed"],
    "designed": ["architected", "engineered", "structured", "modeled"],
    "engineered": ["built", "developed", "architected", "designed"],
    "made": ["built", "created", "developed"],
    "wrote": ["authored", "produced", "drafted"],
    "optimized": ["tuned", "streamlined", "accelerated", "refined"],
    "improved": ["enhanced", "boosted", "strengthened", "refined"],
    "increased": ["boosted", "raised", "grew"],
    "reduced": ["cut", "lowered", "trimmed", "decreased"],
    "led": ["directed", "spearheaded", "drove", "coordinated"],
    "managed": ["ran", "oversaw", "directed", "coordinated"],
    "automated": ["streamlined", "scripted", "orchestrated"],
    "integrated": ["connected", "wired", "incorporated"],
    "deployed": ["shipped", "released", "rolled out"],
    "analyzed": ["examined", "evaluated", "assessed", "investigated"],
    "modeled": ["simulated", "forecasted", "mapped"],
    "tested": ["validated", "verified", "evaluated"],
    "added": ["introduced", "brought", "layered in"],
}


def opening_verb(text: str) -> str:
    m = re.match(r"\s*([A-Za-z][A-Za-z'-]*)", text or "")
    return m.group(1).lower() if m else ""


def _swap_first_word(text: str, new_word: str) -> str:
    m = re.match(r"(\s*)([A-Za-z][A-Za-z'-]*)(.*)", text, re.DOTALL)
    if not m:
        return text
    lead, word, rest = m.groups()
    repl = new_word.capitalize() if word[:1].isupper() else new_word
    return f"{lead}{repl}{rest}"


def dedupe_opening_verbs(bullets: list[str]) -> tuple[list[str], int]:
    """Across the WHOLE resume order, ensure no opening verb repeats — swap repeats for an unused
    synonym of the same sense. Returns (new_bullets, count_changed). Leaves a repeat as-is only if
    it has no known synonyms left."""
    used, out, changed = set(), [], 0
    for b in bullets:
        v = opening_verb(b)
        if v and v in used:
            alt = next((s for s in VERB_SYNONYMS.get(v, []) if s not in used), None)
            if alt:
                b = _swap_first_word(b, alt)
                v = alt
                changed += 1
        if v:
            used.add(v)
        out.append(b)
    return out, changed


# ---------------------------------------------------------------- generic language (#10) ----
_SWAP = {"utilized": "used", "utilize": "use", "utilizing": "using", "utilizes": "uses",
         "leveraged": "used", "leverage": "use", "leveraging": "using", "leverages": "uses"}
_DROP = re.compile(r"\bsuccessfully\s+", re.I)
# Filler/vague words we FLAG (don't auto-rewrite — removing them can change meaning).
_GENERIC_FLAG = re.compile(r"\b(worked on|helped|assisted(?: with)?|responsible for|participated in|"
                           r"contributed to|various|multiple|several|a variety of|in charge of)\b", re.I)


def clean_generic(text: str) -> tuple[str, list[str]]:
    """Apply SAFE deterministic swaps + drops; return (clean_text, remaining_generic_phrases)."""
    def _sub(m):
        w = m.group(0)
        repl = _SWAP[w.lower()]
        return repl.capitalize() if w[:1].isupper() else repl
    out = re.sub(r"\b(" + "|".join(_SWAP) + r")\b", _sub, text, flags=re.I)
    out = _DROP.sub("", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    if out and out[0].islower():        # dropping a leading "Successfully" can lowercase the start
        out = out[0].upper() + out[1:]
    flags = sorted({m.group(0).lower() for m in _GENERIC_FLAG.finditer(out)})
    return out, flags


# ---------------------------------------------------------------- weakness (#6) ----
_WEAK_OPENER = re.compile(r"^\s*(worked on|helped|assisted|responsible for|participated in|contributed to)", re.I)
_PASSIVE = re.compile(r"\b(was|were|been|is|are)\s+\w+ed\b", re.I)
_HAS_NUM = re.compile(r"\d")


def weakness_reasons(text: str, allowed_tech: set[str]) -> list[str]:
    r = []
    if _WEAK_OPENER.search(text):
        r.append("weak opener (worked on / helped / responsible for)")
    if _PASSIVE.search(text):
        r.append("passive voice")
    low = (text or "").lower()
    has_tech = any(jd_signals._present(t, low) for t in allowed_tech) if allowed_tech else False
    if not has_tech and not _HAS_NUM.search(text):
        r.append("no concrete tech or metric")
    return r


# ---------------------------------------------------------------- validation (#13) ----
_BANNED = re.compile(r"\b(as measured by|as evidenced by|as proven by|as indicated by|high accuracy|"
                     r"positive feedback|improved performance|scalable deployment|successful completion|"
                     r"enhanced user experience)\b", re.I)
_NUM = re.compile(r"\b\d[\d,\.]*\b")


def _nums(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM.findall(text or "")}


def validate_bullet(text: str, allowed_tech: set[str], evidence_text: str) -> list[str]:
    """Deterministic red-flags on ONE rewritten bullet. Empty list = clean."""
    issues = []
    ev = (evidence_text or "").lower()
    if _BANNED.search(text):
        issues.append("banned phrase")
    # invented number: a number in the bullet that isn't anywhere in this entry's evidence
    ev_nums = _nums(evidence_text)
    for n in _nums(text):
        if n not in ev_nums and len(n) > 1:        # ignore single digits (years/counts in prose)
            issues.append(f"number '{n}' not in evidence")
    # invented tech: a known tech named in the bullet that's neither allowed nor in the evidence
    low = (text or "").lower()
    allow = {a.lower() for a in allowed_tech}
    for t in jd_signals.TECH_VOCAB:
        if jd_signals._present(t, low) and t not in allow and not jd_signals._present(t, ev):
            issues.append(f"tech '{t}' not in evidence")
    words = len(re.findall(r"\w+", text or ""))
    if words > 34:
        issues.append("too long")
    elif words < 4:
        issues.append("too short")
    return issues


# ------------------------------------------- internal duplication within one bullet ----
# A bullet must never state the same fact twice. The historical cause was builder._ensure_specifics
# re-inserting a "protected" parenthetical it wrongly judged missing (its check was an exact
# substring match, so ANY paraphrase looked like a drop). These helpers are both the detector for
# that class of defect and the coverage test that stops it being re-introduced.

# Words too common to count as evidence that a fact is already stated.
_STOP = {"a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "with", "by", "at", "from",
         "as", "is", "are", "was", "were", "be", "been", "that", "this", "it", "its", "any",
         "all", "per", "via", "using", "used", "into", "over", "up", "out", "one", "top"}


def _content_tokens(text: str) -> set[str]:
    """Meaningful lowercase tokens (words + numbers), stopwords removed. Numbers keep their digits
    only, so '~95-100%' -> {'95', '100'} and matches '95-100%' written any other way."""
    raw = re.findall(r"[a-z0-9][a-z0-9.+#]*", (text or "").lower())
    out = set()
    for t in raw:
        t = t.strip(".")
        if not t or t in _STOP or len(t) < 2 and not t.isdigit():
            continue
        out.add(t)
    return out


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,\.]*", text or "")}


def paren_covered(paren: str, text: str, threshold: float = 0.6) -> bool:
    """Is the substance of `paren` ALREADY stated in `text` (in any wording)?

    This is the check builder._ensure_specifics needs: token-based, not exact-substring, so a
    parenthetical the model legitimately folded into prose is recognised as present instead of being
    re-appended as a duplicate. Also treats a longer parenthetical already in the text as covering a
    shorter overlapping one ('(any image file)' covers '(any image)').
    """
    inner = (paren or "").strip("()").strip()
    ptoks = _content_tokens(inner)
    if not ptoks:
        return True                      # nothing substantive to protect
    ttoks = _content_tokens(text)
    pnums = _numbers(inner)
    if pnums and pnums <= _numbers(text):
        return True                      # every number already stated -> the fact is present
    return len(ptoks & ttoks) / len(ptoks) >= threshold


def fix_unbalanced_parens(text: str) -> str:
    """Drop unmatched parentheses — the rewrite occasionally emits a stray ')' mid-bullet, which
    then reads as corruption once anything is inserted after it."""
    out, depth = [], 0
    for ch in text or "":
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                continue                 # stray closer -> drop
            depth -= 1
        out.append(ch)
    s = "".join(out)
    if depth > 0:                        # unclosed opener(s) -> close at the end
        s = s.rstrip()
        end = "." if s.endswith(".") else ""
        s = (s[:-1] if end else s) + (")" * depth) + end
    return re.sub(r"\s{2,}", " ", s).strip()


def internal_duplication(text: str) -> list[str]:
    """Human-readable reports of a bullet saying the same thing twice. Empty list = clean."""
    issues = []
    t = text or ""
    for paren in re.findall(r"\([^)]{2,}\)", t):
        rest = t.replace(paren, " ")     # the bullet WITHOUT this parenthetical
        if paren_covered(paren, rest):
            issues.append(f"parenthetical {paren} repeats the sentence")
    # a repeated 4+ word run (catches duplication that isn't parenthesised)
    words = re.findall(r"[A-Za-z0-9%~.+#-]+", t)
    low = [w.lower().strip(".,") for w in words]
    seen = {}
    for n in (5, 4):
        for i in range(len(low) - n + 1):
            gram = " ".join(low[i:i + n])
            if len(_content_tokens(gram)) < 2:
                continue
            if gram in seen and not any(gram in s for s in issues):
                issues.append(f"repeated phrase '{gram}'")
            seen[gram] = True
    return issues


def strip_internal_duplication(text: str) -> tuple[str, list[str]]:
    """Remove parentheticals whose substance the bullet already states, then tidy punctuation.
    Returns (clean_text, removed_parentheticals). Only redundant parentheticals are auto-removed —
    a genuinely new specific is never touched; other duplication is reported by
    internal_duplication() for the user to judge."""
    t = fix_unbalanced_parens(text)
    removed = []
    for paren in re.findall(r"\([^)]{2,}\)", t):
        rest = t.replace(paren, " ")
        if paren_covered(paren, rest):
            t = t.replace(" " + paren, "").replace(paren, "")
            removed.append(paren)
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"\s+([,.;:])", r"\1", t)
    t = re.sub(r",\s*\.", ".", t)
    t = re.sub(r"\(\s*\)", "", t)
    return t.strip(), removed


# ------------------------------- unverified claims about this pipeline's own quality ----
# A resume bullet about THIS tool must not assert how WELL it works ("without fabricating skills",
# "accurately", "reliably") unless a test in tests/ actually verifies that property. Describing what
# the tool DOES is always fine; grading its own correctness is a claim that needs evidence.
#
# To make a claim here claimable: add the verifying test, then add its property to
# VERIFIED_CLAIMS with the test's node id. Anything not listed is treated as unproven and stripped.
VERIFIED_CLAIMS: dict[str, str] = {
    # property -> the test that proves it
    "jd_skills_excluded_from_skills_section":
        "tests/test_anti_fabrication.py::test_jd_skills_cannot_reach_the_skills_section",
    "skills_section_stable_across_jds":
        "tests/test_anti_fabrication.py::test_skills_section_is_stable_across_different_jds",
    "no_duplicated_phrasing_in_bullets":
        "tests/test_bullet_duplication.py::test_no_internal_duplication_after_polish",
}

# Quality/correctness assertions about the tool itself. Deliberately narrow in SCOPE (only
# self-referential bullets) but broad in PHRASING — a model will restate the same claim many ways
# ("without fabricating skills", "guaranteeing skills are not fabricated", "prevents hallucinated
# skills"), and each phrasing is the same unproven assertion.
_FABRICATION_WORD = r"(?:fabricat\w+|hallucinat\w+|invent\w+|made[- ]up)"

# Words that GRADE quality rather than describe function — in adverb, adjective and noun form,
# because the model restates the same claim in whichever part of speech fits the sentence
# ("accurately" / "accurate skill representation" / "with high accuracy").
_QUALITY_WORD = (r"(?:accurat\w+|accuracy|correctness|correctly|correct|precisely|precise|"
                 r"reliab\w+|faithful\w*|truthful\w*|honest\w*|flawless\w*|perfectly|perfect|"
                 r"consistent\w*|consistency|robustly|robust|trustworthy|error[- ]free|"
                 r"fidelity)")

_SELF_CLAIM_PATTERNS = [
    # A whole trailing clause built around guaranteeing correctness:
    # "..., guaranteeing skills are not fabricated", "..., ensuring no hallucinations".
    re.compile(rf",?\s*\b(?:guarantee|ensur|assur|verif)\w*\b[^.;]{{0,60}}?{_FABRICATION_WORD}\b"
               r"(?:\s+\w+){0,2}", re.I),
    # Any negated fabrication claim, in either word order.
    re.compile(rf",?\s*\b(?:while|without|never|avoid\w*|prevent\w*|eliminat\w*|no|not)\b"
               rf"[^.;]{{0,40}}?{_FABRICATION_WORD}\b(?:\s+\w+){{0,3}}", re.I),
    re.compile(rf",?\s*{_FABRICATION_WORD}[- ]free\b", re.I),
    re.compile(rf",?\s*\b(?:are|is)\s+(?:not|never)\s+{_FABRICATION_WORD}\b", re.I),
    # A prepositional/participial phrase asserting quality: "with accurate skill representation",
    # "for reliable evidence matching". Strip the whole phrase, not just the adjective, or the
    # sentence is left saying "with skill representation".
    # Consume to the clause boundary (not a fixed word count) so nothing is left as a fragment
    # like "Resume generator evidence matching." Stopping at , ; . keeps any following real fact.
    re.compile(rf",?\s*\b(?:with|for|ensuring|providing|delivering|producing|yielding|maintaining)"
               rf"\s+{_QUALITY_WORD}[^.;,]*", re.I),
    # Bare self-grading adverbs / adjectives / guarantees. Adjective forms matter as much as
    # adverbs: "accurate skill representation" is the same unproven claim as "accurately".
    re.compile(rf"\b{_QUALITY_WORD}\b", re.I),
    re.compile(r"\b(?:guarantee|guarantees|guaranteed|guaranteeing|ensure|ensures|ensured|"
               r"ensuring|assures|assuring)\b", re.I),
    re.compile(r"\b(?:100%|zero)\s+(?:accura\w+|error\w*|hallucinat\w+|fabricat\w+)\b", re.I),
]

# Does this bullet describe the resume/application tool itself?
_SELF_REFERENTIAL_RE = re.compile(
    r"\bresum\w+|\bcover letter\b|\bjob description\b|\bapplication form\b|\bautofill\b|\bats\b",
    re.I)


def is_self_referential(text: str) -> bool:
    """True if the bullet is about this pipeline (a resume/application tool)."""
    return bool(_SELF_REFERENTIAL_RE.search(text or ""))


def self_capability_claims(text: str, verified: set[str] | None = None) -> list[str]:
    """Unproven assertions about how well this tool works. Empty list = nothing to answer for.

    Only applies to self-referential bullets: "reliably" in a bullet about a firefighter VR sim is
    someone else's problem, but in a bullet about this resume generator it is a claim about code we
    can either test or not.
    """
    t = text or ""
    if not is_self_referential(t):
        return []
    if verified is None:
        verified = set(VERIFIED_CLAIMS)
    # A blanket "without fabricating skills" is NOT covered by the narrow skills-section test:
    # bullet prose can still name an out-of-vocabulary tech (see the documented gaps test).
    found = []
    for rx in _SELF_CLAIM_PATTERNS:
        for m in rx.finditer(t):
            found.append(m.group(0).strip())
    return found


def strip_self_capability_claims(text: str) -> tuple[str, list[str]]:
    """Remove unproven self-quality claims, keeping the factual description. Returns
    (clean_text, removed_claims)."""
    t = text or ""
    if not is_self_referential(t):
        return t.strip(), []
    removed = []
    for rx in _SELF_CLAIM_PATTERNS:
        while True:
            m = rx.search(t)
            if not m:
                break
            removed.append(m.group(0).strip())
            t = t[:m.start()] + " " + t[m.end():]
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"\s+([,.;:])", r"\1", t)
    t = re.sub(r",\s*\.", ".", t)
    t = re.sub(r"\s+that\s*\.", ".", t)
    t = t.strip()
    if t and not t.endswith("."):
        t += "."
    return t, removed


# ------------------------------- duplication ACROSS bullets in the same entry ----
# The within-a-bullet fix did not cover this: two bullets under one entry can each be a rewrite of
# the SAME source fact ("React Native app on Firebase delivering real-time multi-floor navigation"
# twice), which reads as two accomplishments and silently drops whichever source fact went
# uncovered. Detection is deterministic here; builder pairs it with embedding similarity so
# near-paraphrase is caught too.

# Words that carry a bullet's actual content. A bullet earns its place by introducing at least one
# of these that no earlier bullet in the same entry already used.
_UNIT_STOP = _STOP | {
    "built", "build", "building", "developed", "develop", "created", "create", "designed", "design",
    "engineered", "shipped", "delivered", "deliver", "delivering", "implemented", "led", "leading",
    "made", "wrote", "added", "enabling", "enabled", "enable", "using", "use", "used", "app",
    "application", "project", "team", "work", "worked", "new", "full", "end", "across", "through",
    "students", "student", "users", "user", "company", "school", "real", "time", "multi", "based",
    "cross", "platform", "standalone", "complete", "efficient", "small",
}


def content_units(text: str) -> set[str]:
    """The distinctive content of a bullet: tech names, numbers, and specific nouns — with generic
    resume verbs and shared context words removed, since those repeat legitimately."""
    units = set(_numbers(text))
    low = (text or "").lower()
    for t in jd_signals.TECH_VOCAB:
        if len(t) > 1 and jd_signals._present(t, low):
            units.add(t)
    for tok in re.findall(r"[a-z][a-z0-9.+#/-]{2,}", low):
        tok = tok.strip(".-/")
        if tok and tok not in _UNIT_STOP and len(tok) > 2:
            units.add(tok)
    return units


def bullet_overlap(a: str, b: str) -> float:
    """Jaccard overlap of two bullets' content units (0..1). Verb/context words are excluded, so a
    high score means the same FACTS, not merely similar sentence shape."""
    ua, ub = content_units(a), content_units(b)
    if not ua or not ub:
        return 0.0
    return len(ua & ub) / len(ua | ub)


def new_units(bullet: str, prior: list[str]) -> set[str]:
    """Content units this bullet adds that no earlier bullet in the entry already covered."""
    covered = set()
    for p in prior:
        covered |= content_units(p)
    return content_units(bullet) - covered


def redundant_pairs(bullets: list[str], threshold: float = 0.45) -> list[dict]:
    """Pairs of bullets in one entry that cover the same ground. Reports the later index as the
    redundant one (the first statement of a fact keeps priority)."""
    out = []
    for i in range(len(bullets)):
        for j in range(i + 1, len(bullets)):
            ov = bullet_overlap(bullets[i], bullets[j])
            if ov >= threshold:
                out.append({"keep": i, "redundant": j, "overlap": round(ov, 3),
                            "shared": sorted(content_units(bullets[i]) & content_units(bullets[j]))})
    return out


def tech_units(text: str) -> set[str]:
    low = (text or "").lower()
    return {t for t in jd_signals.TECH_VOCAB if len(t) > 1 and jd_signals._present(t, low)}


def bullets_adding_nothing(bullets: list[str], overlap_threshold: float = 0.4) -> list[int]:
    """Indices of bullets that restate an earlier bullet instead of advancing to a new fact.

    Counting *any* new word is too lenient: a second rewrite of one fact still introduces words
    like "cross-platform", "mobile", "turn-by-turn" — adjectives re-describing the SAME feature.
    So a bullet earns its place only by introducing a new TECHNOLOGY or a new NUMBER; failing that,
    it is redundant if it substantially overlaps a bullet that came before it.
    """
    out = []
    for i, b in enumerate(bullets):
        if i == 0:
            continue
        prior = bullets[:i]
        prior_tech, prior_nums = set(), set()
        for p in prior:
            prior_tech |= tech_units(p)
            prior_nums |= _numbers(p)
        adds_tech = bool(tech_units(b) - prior_tech)
        adds_number = bool(_numbers(b) - prior_nums)
        if adds_tech or adds_number:
            continue
        if max((bullet_overlap(b, p) for p in prior), default=0.0) >= overlap_threshold:
            out.append(i)
    return out


# ---------------------------------------------------------------- ordering (#4) ----
def order_bullets(bullets: list[str], jd_terms: set[str]) -> list[str]:
    """Strongest first, deterministically: impact + JD keyword overlap. Stable for ties, so a given
    set of rewritten bullets always orders the same way across runs."""
    def key(b):
        low = b.lower()
        overlap = sum(1 for t in jd_terms if jd_signals._present(t, low))
        return (impact_score(b) + 0.5 * overlap)
    return sorted(bullets, key=key, reverse=True)


# ---------------------------------------------------------------- ATS report (#8) ----
def ats_report(content: dict, jd_analysis: dict) -> dict:
    sig = jd_signals.categorize(jd_analysis)
    req = [s.lower() for s in sig["required_skills"]]
    cats = sig["categories"]

    bullets = []
    for e in content.get("work", []) or []:
        bullets += e.get("bullets", []) or []
    for p in content.get("projects", []) or []:
        bullets += p.get("bullets", []) or []
    skills_text = " ".join(
        " ".join(v) for v in (content.get("skills") or {}).values()).lower()
    resume_blob = (" ".join(bullets) + " " + skills_text).lower()

    covered = [s for s in req if jd_signals._present(s, resume_blob)]
    cat_cov = {c: any(jd_signals._present(t, resume_blob) for t in terms) for c, terms in cats.items()}

    verbs = [opening_verb(b) for b in bullets if b.strip()]
    verb_div = round(len(set(verbs)) / len(verbs), 2) if verbs else 0.0
    lengths = [len(re.findall(r"\w+", b)) for b in bullets if b.strip()]
    avg_len = round(sum(lengths) / len(lengths), 1) if lengths else 0.0
    with_metric = sum(1 for b in bullets if _HAS_NUM.search(b))
    metric_frac = round(with_metric / len(bullets), 2) if bullets else 0.0

    # keyword repetition (stuffing): count each required skill across bullets, flag > 3
    rep = {}
    for s in req:
        c = sum(1 for b in bullets if jd_signals._present(s, b.lower()))
        if c:
            rep[s] = c
    stuffed = sorted([s for s, c in rep.items() if c > 3])

    skills_cov = len(covered) / len(req) if req else 1.0
    relevance = round(100 * (0.6 * skills_cov + 0.2 * min(1.0, verb_div * 1.25)
                             + 0.2 * min(1.0, metric_frac * 2)), 1)
    return {
        "required_skills": sig["required_skills"],
        "required_covered": [s for s in sig["required_skills"] if s.lower() in covered],
        "required_missing": [s for s in sig["required_skills"] if s.lower() not in covered],
        "skills_coverage_pct": round(100 * skills_cov, 1),
        "category_coverage": cat_cov,
        "action_verb_diversity": verb_div,
        "avg_bullet_words": avg_len,
        "bullets_with_metric_pct": round(100 * metric_frac, 1),
        "keyword_stuffed": stuffed,
        "ats_relevance_estimate": relevance,
    }
