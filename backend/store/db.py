"""SQLite store for Q&A memory, the job tracker, and autofill site memory.

Single-user, single file at ~/ResumeRewriter/rr.db. On first open we migrate the v1 JSON
stores (qa_memory.json, tracker.json, site_memory.json) in, once, by id. The JSON files are
left untouched as a fallback.
"""
import json
import sqlite3
import threading
from .. import config

_local = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS qa_memory (
    id           TEXT PRIMARY KEY,
    question     TEXT NOT NULL,
    answer       TEXT NOT NULL,
    field_type   TEXT,
    embedding    TEXT,            -- json array of floats
    created_at   TEXT,
    usage_count  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tracker (
    id                    TEXT PRIMARY KEY,
    company               TEXT,
    role                  TEXT,
    jd_text               TEXT,
    focus_angle           TEXT,
    folder                TEXT,
    pdf_filename          TEXT,
    cover_letter_filename TEXT,
    status                TEXT,
    created_at            TEXT,
    content               TEXT,   -- json
    jd_analysis           TEXT,   -- json
    jd_fingerprint        TEXT
);
CREATE TABLE IF NOT EXISTS site_memory (
    id            TEXT PRIMARY KEY,
    site_key      TEXT,
    hostname      TEXT,
    ats           TEXT,
    display_name  TEXT,
    tips          TEXT,           -- json
    selectors     TEXT,           -- json
    notes         TEXT,
    success_count INTEGER DEFAULT 0,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS startups (
    id                  TEXT PRIMARY KEY,   -- sha1(domain)[:12]
    name                TEXT,
    website             TEXT,
    domain              TEXT,
    one_liner           TEXT,
    description         TEXT,
    team_size           INTEGER,
    stage               TEXT,
    batch               TEXT,
    industry            TEXT,
    subindustry         TEXT,
    tags                TEXT,               -- json array
    regions             TEXT,               -- json array
    location            TEXT,
    is_hiring           INTEGER,
    hiring_signals      TEXT,               -- json array of human-readable strings
    source              TEXT,
    source_url          TEXT,
    fit_score           INTEGER,
    fit_reason          TEXT,
    hiring_score        INTEGER,
    hiring_label        TEXT,
    contact_name        TEXT,
    contact_title       TEXT,
    contact_linkedin    TEXT,
    contact_role_reco   TEXT,
    contact_search_url  TEXT,
    status              TEXT,               -- new | saved | skipped | contacted
    discovered_at       TEXT,
    last_seen           TEXT,
    raw                 TEXT,               -- json
    intel               TEXT,               -- json: {dossier, why_this_company, best_angle, ...}
    intel_at            TEXT                -- when the intel was generated (cache stamp)
);
CREATE TABLE IF NOT EXISTS startup_contacts (
    id               TEXT PRIMARY KEY,   -- sha1(startup_id + profile_url|name)[:12]
    startup_id       TEXT,
    name             TEXT,
    title            TEXT,
    role_bucket      TEXT,               -- founder_cto | eng_manager | engineer | recruiter | other
    profile_url      TEXT,               -- verification link: GitHub/YC/HN profile or post
    linkedin_url     TEXT,               -- a specific, real (or precisely name-targeted) LinkedIn
    source           TEXT,               -- YC | GitHub | HN
    location         TEXT,
    company          TEXT,
    email            TEXT,               -- reserved for the future email phase (nullable now)
    email_source     TEXT,
    relationship     TEXT,               -- json array of shared-connection highlight strings
    authority_score  INTEGER,
    relevance_score  INTEGER,
    score            INTEGER,
    is_recommended   INTEGER,
    created_at       TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(config.DB_PATH)
        c.row_factory = sqlite3.Row
        c.executescript(_SCHEMA)
        _migrate_columns(c)
        _migrate(c)
        _local.conn = c
    return c


def _migrate_columns(c: sqlite3.Connection) -> None:
    """Additive column migrations for DBs created before the column existed."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(tracker)")}
    if "sent_at" not in cols:
        c.execute("ALTER TABLE tracker ADD COLUMN sent_at TEXT")
    if "ask_history" not in cols:
        c.execute("ALTER TABLE tracker ADD COLUMN ask_history TEXT")   # JSON [{question,answer,at}]
    if "skills_embedding" not in cols:
        # Vestigial: fed the removed resume-reuse cache (dropped 2026-08-17). Nothing reads or
        # writes it now; the column stays only because existing DBs have it and SQLite makes
        # dropping a column a table rebuild for no benefit.
        c.execute("ALTER TABLE tracker ADD COLUMN skills_embedding TEXT")
    if "contact_name" not in cols:
        # recruiter/contact + drafted outreach for THIS application (never auto-sent).
        for col in ("contact_name", "contact_title", "contact_linkedin", "contact_role_reco",
                    "contact_search_url", "msg_subject", "msg_email", "msg_linkedin",
                    "msg_type", "msg_angle", "msg_at"):
            c.execute(f"ALTER TABLE tracker ADD COLUMN {col} TEXT")
    # startups.intel — company-intelligence dossier + LLM "why this company" (outreach Phase 2)
    scols = {r["name"] for r in c.execute("PRAGMA table_info(startups)")}
    if scols and "intel" not in scols:
        c.execute("ALTER TABLE startups ADD COLUMN intel TEXT")
        c.execute("ALTER TABLE startups ADD COLUMN intel_at TEXT")
    # startup_contacts message drafts (outreach Phase 4)
    ccols = {r["name"] for r in c.execute("PRAGMA table_info(startup_contacts)")}
    if ccols and "msg_email" not in ccols:
        for col in ("msg_subject", "msg_email", "msg_linkedin", "msg_type", "msg_angle", "msg_at"):
            c.execute(f"ALTER TABLE startup_contacts ADD COLUMN {col} TEXT")
    if ccols and "linkedin_url" not in ccols:
        c.execute("ALTER TABLE startup_contacts ADD COLUMN linkedin_url TEXT")
    c.commit()


def _migrate(c: sqlite3.Connection) -> None:
    if c.execute("SELECT v FROM meta WHERE k='v1_migrated'").fetchone():
        return
    _migrate_qa(c)
    _migrate_tracker(c)
    _migrate_site(c)
    c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('v1_migrated','1')")
    c.commit()


def _load_json_list(path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _migrate_qa(c):
    # Re-embed from question text with the CURRENT model so all vectors share one space.
    # v1 embeddings were generated differently and aren't comparable to fresh ones.
    from .. import embeddings
    for r in _load_json_list(config.LEGACY_QA_PATH):
        q = r.get("question", "")
        try:
            emb = embeddings.embed(q) if q.strip() else []
        except Exception:
            emb = r.get("embedding") or []
        c.execute(
            "INSERT OR IGNORE INTO qa_memory(id,question,answer,field_type,embedding,created_at,usage_count)"
            " VALUES(?,?,?,?,?,?,?)",
            (r.get("id"), q, r.get("answer", ""), r.get("field_type"),
             json.dumps(emb), r.get("created_at"), r.get("usage_count", 0)),
        )


def _migrate_tracker(c):
    for r in _load_json_list(config.LEGACY_TRACKER_PATH):
        c.execute(
            "INSERT OR IGNORE INTO tracker(id,company,role,jd_text,focus_angle,folder,pdf_filename,"
            "cover_letter_filename,status,created_at,content,jd_analysis,jd_fingerprint)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r.get("id"), r.get("company"), r.get("role"), r.get("jd_text"), r.get("focus_angle"),
             r.get("folder"), r.get("pdf_filename"), r.get("cover_letter_filename"),
             r.get("status"), r.get("created_at"),
             json.dumps(r.get("content")), json.dumps(r.get("jd_analysis")),
             r.get("jd_fingerprint")),
        )


def _migrate_site(c):
    for r in _load_json_list(config.LEGACY_SITE_MEMORY_PATH):
        c.execute(
            "INSERT OR IGNORE INTO site_memory(id,site_key,hostname,ats,display_name,tips,"
            "selectors,notes,success_count,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (r.get("id"), r.get("site_key"), r.get("hostname"), r.get("ats"),
             r.get("display_name"), json.dumps(r.get("tips") or []),
             json.dumps(r.get("selectors") or {}), r.get("notes"),
             r.get("success_count", 0), r.get("created_at"), r.get("updated_at")),
        )
