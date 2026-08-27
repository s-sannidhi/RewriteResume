"""Single source of truth for the resume's Skills section.

Why this module exists: the Skills section used to be assembled from three places at once — the
profile's curated `skills`, JD-matched promotions out of `skills_extra`, and a "close family"
transfer that added a JD skill the candidate did NOT have whenever they had a sibling of it (having
React added Angular). That made the section vary between generations of the same resume and let
skills appear that the candidate could not defend in an interview.

Now: the ONLY input is `~/ResumeRewriter/skills_verified.yaml`, maintained by hand. Nothing else may
add to the Skills section — not the job description, not the LLM, not a similarity heuristic. The
generator may only ORDER what is in this file (JD-relevant first), never extend it.

File shape:

    verified:
      <group>:
        - name: Python
          evidence: [ML Racecar, AI Resume & Outreach Agent]   # entries that demonstrate it
    needs_review:        # IGNORED by the generator; staging area to prune or justify
      <group>:
        - name: PostgreSQL
          evidence: []
"""
import re

from .. import config

SKILLS_PATH = config.DATA_DIR / "skills_verified.yaml"

# Rendering order for groups that appear in the file; unknown groups follow, alphabetically.
GROUP_ORDER = ["programming_languages", "frameworks", "ml_ai", "ai_tools", "databases", "cloud",
               "tools"]

_cache: dict | None = None

# Starter file written on a fresh install (setup.py). Also the fingerprint that lets a data
# restore overwrite an untouched placeholder without a --force clash — see scripts/backup_data.py.
TEMPLATE = """\
# The ONLY source of skills on your resume. The job description can reorder this list;
# nothing can add to it - not the LLM, not a keyword match.
#
# Starts empty on purpose: an example skill left in here would end up on a real resume.
# List only what you could defend in an interview, and name the project, job or course
# that proves it under evidence:. Uncomment the block below and edit it.
verified: {}
#  programming_languages:
#    - name: "Python"
#      evidence: ["<a project or job that is on your resume>"]
#    - name: "Java"
#      evidence: ["Data Structures"]        # coursework counts
#  frameworks: []
#  databases: []
#  cloud: []
#  ml_ai: []
#  tools: []

# Staging area. Ignored by the generator - move a skill up into verified: once you can
# point at real evidence for it, or delete it.
needs_review: {}
"""


def ensure_template() -> bool:
    """Create the starter file if there isn't one. True if it was just written.

    A fresh clone has no ~/ResumeRewriter at all, and an app that answers "no skills file" with
    an exception looks broken. A template the user edits is a far better first contact.
    """
    if SKILLS_PATH.exists():
        return False
    SKILLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SKILLS_PATH.write_text(TEMPLATE, encoding="utf-8")
    reload()
    return True


def is_untouched_template() -> bool:
    """True when the file is still exactly the seeded starter — nothing of the user's in it."""
    try:
        return SKILLS_PATH.read_text(encoding="utf-8") == TEMPLATE
    except OSError:
        return False


class SkillsSourceError(RuntimeError):
    """The verified-skills file is missing or unreadable. Generation must not silently guess."""


def _load_raw() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        import yaml
    except ImportError as e:      # pragma: no cover - dependency is in requirements.txt
        raise SkillsSourceError("PyYAML is not installed (pip install -r requirements.txt)") from e
    if not SKILLS_PATH.exists():
        raise SkillsSourceError(
            f"No verified-skills file at {SKILLS_PATH}. The Skills section is intentionally "
            "sourced only from this hand-maintained file — create it (see "
            "backend/resume/skills_source.py for the format) rather than letting the generator "
            "guess which skills are defensible.")
    try:
        data = yaml.safe_load(SKILLS_PATH.read_text(encoding="utf-8")) or {}
    except Exception as e:
        raise SkillsSourceError(f"Could not parse {SKILLS_PATH}: {e}") from e
    if not isinstance(data, dict):
        raise SkillsSourceError(f"{SKILLS_PATH} must be a mapping with a 'verified:' key")
    _cache = data
    return _cache


def reload() -> None:
    """Drop the cache so an edit to the file takes effect without a restart."""
    global _cache
    _cache = None


def _entries(section: str) -> dict[str, list[dict]]:
    data = _load_raw()
    block = data.get(section) or {}
    out: dict[str, list[dict]] = {}
    if not isinstance(block, dict):
        return out
    for group, items in block.items():
        rows = []
        for it in items or []:
            if isinstance(it, str):                  # bare string = no evidence recorded
                rows.append({"name": it.strip(), "evidence": []})
            elif isinstance(it, dict) and (it.get("name") or "").strip():
                ev = it.get("evidence") or []
                ev = [ev] if isinstance(ev, str) else list(ev)
                rows.append({"name": str(it["name"]).strip(),
                             "evidence": [str(x).strip() for x in ev if str(x).strip()]})
        if rows:
            out[str(group)] = rows
    return out


def verified() -> dict[str, list[dict]]:
    """{group: [{name, evidence}]} — the only skills allowed on the resume."""
    return _entries("verified")


def needs_review() -> dict[str, list[dict]]:
    """Quarantined skills. Never rendered; surfaced so they get pruned or justified."""
    return _entries("needs_review")


def ordered_groups(jd_keywords: list[str] | None = None) -> dict[str, list[str]]:
    """The Skills section: verified names only, JD-relevant first within each group.

    Ordering is the ONLY influence the job description has here. No JD keyword can add a skill.
    """
    jd = {k.strip().lower() for k in (jd_keywords or []) if k and k.strip()}
    src = verified()
    out: dict[str, list[str]] = {}
    groups = [g for g in GROUP_ORDER if g in src] + sorted(g for g in src if g not in GROUP_ORDER)
    for group in groups:
        seen, names = set(), []
        for row in src[group]:
            key = row["name"].lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(row["name"])
        names.sort(key=lambda s: s.lower() not in jd)      # stable: JD-relevant first
        if names:
            out[group] = names
    return out


def _rendered_evidence_labels(content: dict) -> set[str]:
    """Lowercased identifiers of everything actually ON this resume that a skill can point at:
    company names, job titles, project names — and coursework, since a language learned in a class
    (Data Structures is taught in Java) is legitimate evidence and the course line is on the page."""
    labels = set()
    for w in (content or {}).get("work", []) or []:
        for k in ("company", "title"):
            if (w.get(k) or "").strip():
                labels.add(w[k].strip().lower())
    for p in (content or {}).get("projects", []) or []:
        if (p.get("name") or "").strip():
            labels.add(p["name"].strip().lower())
    for ed in (content or {}).get("education", []) or []:
        for c in (ed.get("coursework") or []):
            if (c or "").strip():
                labels.add(c.strip().lower())
        if (ed.get("school") or "").strip():
            labels.add(ed["school"].strip().lower())
    return labels


def evidence_warnings(content: dict) -> list[dict]:
    """Verified skills whose linked evidence is NOT on this particular resume.

    Two distinct problems, both worth telling the user about at generation time:
      * no_evidence_recorded — the file lists the skill with an empty `evidence:` list, so nothing
        in the profile backs it up at all;
      * evidence_not_on_resume — the skill's evidence exists, but none of those entries rendered
        (only the top-2 projects make the resume), so the skill is claimed with nothing supporting
        it on the page a recruiter reads.
    """
    rendered = _rendered_evidence_labels(content)
    shown = {s.lower() for items in (content.get("skills") or {}).values() for s in items}
    out = []
    for group, rows in verified().items():
        for row in rows:
            if row["name"].lower() not in shown:
                continue                     # not on this resume; nothing to warn about
            if not row["evidence"]:
                out.append({"skill": row["name"], "group": group,
                            "issue": "no_evidence_recorded",
                            "detail": "listed as verified but has no evidence: entries"})
                continue
            if not any(_label_matches(e, rendered) for e in row["evidence"]):
                out.append({"skill": row["name"], "group": group,
                            "issue": "evidence_not_on_resume",
                            "detail": f"evidence {row['evidence']} is not among the experiences "
                                      f"shown on this resume"})
    return out


def _label_matches(evidence_name: str, rendered: set[str]) -> bool:
    e = (evidence_name or "").strip().lower()
    if not e:
        return False
    return any(e == r or e in r or r in e for r in rendered)


def unknown_skill_report(content: dict) -> list[str]:
    """Skills present in rendered content but NOT in the verified file — should always be empty.
    A non-empty result means something bypassed this module and is a bug, not a warning."""
    allowed = {row["name"].lower() for rows in verified().values() for row in rows}
    shown = {s for items in (content.get("skills") or {}).values() for s in items}
    return sorted(s for s in shown if s.lower() not in allowed)
