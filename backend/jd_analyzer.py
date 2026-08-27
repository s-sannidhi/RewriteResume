"""Analyze a job description that the EXTENSION read off the page (never pasted by the user).

Produces: company/role/seniority, a tailoring brief, focus-angle suggestions, and a keyword
recommender that surfaces ONLY concrete, installable/importable/invocable tools that appear in
the JD and are missing from the profile. Concepts/buzzwords are filtered out.
"""
import re
import time
from . import config, llm, profile_store

# Concept/buzzword denylist — these are never "skills" (you can't install/import/invoke them).
_CONCEPT_BLOCK = {
    "machine learning", "deep learning", "artificial intelligence", "ai", "ml", "nlp",
    "distributed systems", "data structures", "algorithms", "object oriented programming",
    "oop", "ci/cd", "cicd", "agile", "scrum", "devops", "mlops", "microservices",
    "cloud computing", "big data", "data science", "full stack", "frontend", "backend",
    "web development", "software engineering", "problem solving", "communication",
    "teamwork", "leadership", "computer science", "rest", "api", "apis", "sql databases",
    "version control", "testing", "debugging", "data analysis", "automation", "security",
    "networking", "operating systems",
}

_SKILL_GROUPS = (
    "programming_languages", "frameworks", "ml_ai", "tools", "cloud", "databases",
    "hardware_embedded",
)


def is_concrete_skill(term: str) -> bool:
    """A valid skill is something you can install, import, or directly invoke."""
    t = term.strip().lower()
    if not t or len(t) < 2 or len(t) > 40:
        return False
    if t in _CONCEPT_BLOCK:
        return False
    # multi-word phrases that are clearly concepts (contain a blocked head word)
    if any(t == c or t.startswith(c + " ") or t.endswith(" " + c) for c in _CONCEPT_BLOCK):
        return False
    return True


_ANALYZE_SYS = (
    "You analyze software job descriptions for a CS new-grad applicant. "
    "Return strict JSON. Be precise and do not invent requirements not in the text."
)


def analyze(jd_text: str) -> dict:
    """LLM pass over the JD: structured facts + concrete tech mentions + angle ideas."""
    from . import timing
    timing.reset("analyze")
    t0 = time.time()
    profile = profile_store.load()
    have = profile_store.all_skills(profile)
    blocked = profile_store.blocklist(profile)

    cap = getattr(config, "JD_ANALYZE_CHARS", 5000)
    user = f"""Job description:
\"\"\"
{jd_text[:cap]}
\"\"\"

Return JSON with exactly these keys:
- "company": string (best guess, "" if unknown)
- "role_title": string
- "seniority": one of "intern","new_grad","junior","mid","senior","unknown"
- "summary": 1-2 sentence plain summary of what the role wants
- "concrete_tech": array of specific technologies/tools/languages/frameworks explicitly named
  in the JD (e.g. "React","PostgreSQL","Kubernetes"). NO concepts like "machine learning",
  "microservices","CI/CD","distributed systems". Only things you can install/import/invoke.
- "responsibilities": array of up to 6 short strings
- "angle_ideas": array of 2-4 short tailoring angles for slanting a resume to THIS job
  (e.g. "Backend reliability & APIs", "ML systems & data pipelines")."""

    with timing.stage("analyze_llm"):
        data = llm.chat_json(_ANALYZE_SYS, user, num_predict=config.LLM_PREDICT_ANALYZE)

    # Keyword recommender: concrete tech in JD, not already in profile, not a concept.
    with timing.stage("analyze_other"):
        recs = []
        seen = set()
        for term in data.get("concrete_tech", []) or []:
            if not isinstance(term, str):
                continue
            key = term.strip().lower()
            if key in seen or key in have or key in blocked or not is_concrete_skill(term):
                continue
            seen.add(key)
            recs.append({"term": term.strip(), "suggested_group": _guess_group(term)})

        data["keyword_recommendations"] = recs
        # Always offer the user's saved angle presets alongside JD-derived ones.
        presets = (profile.get("ai_preferences") or {}).get("default_focus_angles") or []
        ideas = list(dict.fromkeys((data.get("angle_ideas") or []) + presets))
        data["angle_ideas"] = ideas
    timing.log_summary(time.time() - t0)
    return data


_GROUP_HINTS = {
    "programming_languages": {"python", "java", "javascript", "typescript", "c++", "c#", "go",
                              "rust", "ruby", "kotlin", "swift", "scala", "php", "c"},
    "databases": {"postgresql", "postgres", "mysql", "mongodb", "redis", "sqlite", "oracle",
                  "dynamodb", "cassandra", "firebase", "mssql"},
    "cloud": {"aws", "azure", "gcp", "google cloud"},
    "ml_ai": {"pytorch", "tensorflow", "scikit-learn", "keras", "pandas", "numpy", "hugging face"},
}


# Skills most tech recruiters recognize at a glance. Membership = "general" (fast path).
# This is an ALLOWLIST of common tools, NOT a niche blocklist — anything not here is judged
# by the model, and unknowns default to niche ("ask how it was used").
_MAINSTREAM = {
    "python", "java", "javascript", "typescript", "c", "c++", "c#", "go", "golang", "ruby",
    "php", "swift", "kotlin", "rust", "scala", "r", "sql", "html", "html5", "css", "sass",
    "react", "react native", "node.js", "node", "next.js", "express", "django", "flask",
    "fastapi", "spring", "spring boot", "angular", "vue", "svelte", ".net", "rails", "laravel",
    "tailwind css", "bootstrap", "jquery", "expo", "flutter", "android", "ios",
    "mysql", "postgresql", "postgres", "mongodb", "sqlite", "redis", "firebase", "oracle",
    "microsoft sql server", "mssql", "mariadb", "dynamodb", "cassandra", "elasticsearch",
    "aws", "azure", "gcp", "google cloud", "docker", "kubernetes", "linux", "unix", "bash",
    "git", "github", "gitlab", "bitbucket", "jenkins", "npm", "yarn", "webpack", "vite",
    "terraform", "pytorch", "tensorflow", "keras", "scikit-learn", "numpy", "pandas",
    "matplotlib", "jupyter", "jira", "figma", "postman", "rest", "graphql",
}

_NICHE_SYS = ("You classify a software skill as mainstream (most tech recruiters recognize it) "
              "or niche (specialized, domain-specific, or older/uncommon). Return strict JSON.")


def classify_niche(term: str) -> dict:
    """Decide if a skill is niche (needs to be demonstrated inside an experience) or general."""
    t = (term or "").strip().lower()
    if not t:
        return {"niche": False, "reason": "empty"}
    if t in _MAINSTREAM:
        return {"niche": False, "reason": "mainstream"}
    try:
        d = llm.chat_json(
            _NICHE_SYS,
            f'Classify the software skill "{term}". Treat it as NICHE if it is specialized or '
            "domain-specific (GIS, embedded/hardware, big-data or data-engineering, scientific/"
            "numerical computing), legacy/older, or something a GENERALIST software-engineering "
            "recruiter might not immediately recognize. Treat it as MAINSTREAM only if it is "
            "broadly used across general software roles. "
            'Return JSON: {"niche": true or false, "reason": "<= 8 words"}.',
        )
        return {"niche": bool(d.get("niche")), "reason": (d.get("reason") or "")[:60]}
    except Exception:
        return {"niche": True, "reason": "uncertain — treat as niche"}


def _guess_group(term: str) -> str:
    t = term.strip().lower()
    for group, members in _GROUP_HINTS.items():
        if t in members:
            return group
    if any(k in t for k in ("db", "sql")):
        return "databases"
    return "frameworks"  # safe default for libraries/frameworks/tools
