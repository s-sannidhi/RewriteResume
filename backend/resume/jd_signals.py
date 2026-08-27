"""Deterministic structured signals from a JD — NO LLM. Turns the analyzer's concrete_tech + a
scan of the JD text into CATEGORY buckets (languages, frameworks, cloud, … leadership,
communication). Used to prioritize evidence (evidence.py) and score ATS coverage (quality.py).
"""
import re

# category -> the terms that signal it. Plain lowercase; multiword matched as substrings, single
# word-ish tokens matched on tech-aware boundaries so "go" != "google" and "c" != "css".
CATEGORY_TERMS: dict[str, list[str]] = {
    "languages": ["python", "java", "javascript", "typescript", "c++", "c#", "c", "go", "golang",
                  "rust", "ruby", "kotlin", "swift", "scala", "php", "sql", "bash", "shell", "r"],
    "frameworks": ["react", "react native", "angular", "vue", "svelte", "next.js", "node", "node.js",
                   "express", "django", "flask", "fastapi", "spring", "spring boot", "rails", ".net",
                   "flutter", "tailwind", "bootstrap", "redux"],
    "cloud": ["aws", "gcp", "google cloud", "azure", "lambda", "s3", "ec2", "cloudflare", "heroku",
              "vercel", "firebase"],
    "databases": ["postgresql", "postgres", "mysql", "mongodb", "redis", "sqlite", "dynamodb",
                  "cassandra", "elasticsearch", "oracle", "sql server", "mariadb"],
    "infrastructure": ["docker", "kubernetes", "k8s", "terraform", "jenkins", "ci/cd", "cicd",
                       "github actions", "ansible", "nginx", "kafka", "rabbitmq", "grpc", "helm",
                       "prometheus", "grafana", "devops"],
    "testing": ["jest", "pytest", "junit", "selenium", "cypress", "playwright", "mocha",
                "unit test", "integration test", "tdd", "test coverage", "testing"],
    "distributed_systems": ["distributed", "microservice", "microservices", "scalable",
                            "scalability", "high-throughput", "throughput", "low-latency", "latency",
                            "concurrency", "concurrent", "sharding", "replication", "message queue",
                            "event-driven", "fault-tolerant", "load balanc"],
    "machine_learning": ["pytorch", "tensorflow", "scikit-learn", "sklearn", "keras",
                         "machine learning", "deep learning", "nlp", "llm", "neural", "regression",
                         "classification", "classifier", "pandas", "numpy", "hugging face",
                         "embeddings", "model training"],
    "frontend": ["react", "vue", "angular", "svelte", "css", "html", "tailwind", "ui", "ux",
                 "frontend", "front-end", "responsive", "accessibility", "figma", "redux"],
    "backend": ["api", "apis", "rest", "restful", "graphql", "backend", "back-end", "server",
                "endpoint", "microservice", "orm", "authentication", "caching"],
    "leadership": ["lead", "led", "leader", "mentor", "mentored", "manage", "managed", "own",
                   "owned", "ownership", "spearhead", "drove", "coordinate", "roadmap"],
    "communication": ["communicat", "collaborat", "present", "cross-functional", "documentation",
                      "articulate", "teamwork", "authored", "wrote", "stakeholder"],
}

# Everything you can install/import/invoke — used to spot an INVENTED tech in a rewritten bullet.
TECH_VOCAB: set[str] = set()
for _cat in ("languages", "frameworks", "cloud", "databases", "infrastructure", "testing",
             "machine_learning"):
    TECH_VOCAB |= {t for t in CATEGORY_TERMS[_cat]}


def _present(term: str, blob: str) -> bool:
    """Whole-token match for word-ish terms; substring for multiword/punctuated ones."""
    if " " in term or any(c in term for c in "+#./-"):
        return term in blob
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", blob) is not None


def blob_of(jd_analysis: dict) -> str:
    parts = [
        jd_analysis.get("role_title", ""), jd_analysis.get("summary", ""),
        " ".join(jd_analysis.get("responsibilities") or []),
        " ".join(jd_analysis.get("concrete_tech") or []),
        (jd_analysis.get("jd_text", "") or "")[:4000],
    ]
    return " ".join(parts).lower()


def categorize(jd_analysis: dict) -> dict:
    """{category: [matched terms]} for every category the JD touches, plus the concrete required
    skills. Purely deterministic."""
    blob = blob_of(jd_analysis)
    cats = {}
    for cat, terms in CATEGORY_TERMS.items():
        hit = [t for t in terms if _present(t, blob)]
        if hit:
            cats[cat] = sorted(set(hit))
    return {
        "categories": cats,
        "required_skills": [t.strip() for t in (jd_analysis.get("concrete_tech") or []) if t.strip()],
    }


def jd_query(jd_analysis: dict, focus_angle: str = "") -> str:
    """A compact string to embed for evidence ranking — the tech + what the role does + the angle."""
    tech = ", ".join(jd_analysis.get("concrete_tech") or [])
    resp = ". ".join((jd_analysis.get("responsibilities") or [])[:6])
    bits = [focus_angle, jd_analysis.get("role_title", ""), tech,
            jd_analysis.get("summary", ""), resp]
    return ". ".join(b for b in bits if b).strip()


def jd_terms(jd_analysis: dict) -> set[str]:
    """Flat set of all JD signal terms (for cheap keyword-overlap scoring of rewritten bullets)."""
    cats = categorize(jd_analysis)["categories"]
    out = set()
    for terms in cats.values():
        out |= set(terms)
    out |= {t.strip().lower() for t in (jd_analysis.get("concrete_tech") or []) if t.strip()}
    return out
