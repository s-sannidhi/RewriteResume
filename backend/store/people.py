"""People / contacts store (outreach Phase 3). One row per person, FK to a startup.

A discovery run for a company REPLACES that company's contacts (delete + insert) so re-running
always reflects the latest ranking. Email columns exist but stay empty — a later phase fills them.
"""
import hashlib
import json
import time
from . import db

ROLE_BUCKETS = ("founder_cto", "eng_manager", "engineer", "recruiter", "other")

_FIELDS = ("name", "title", "role_bucket", "profile_url", "linkedin_url", "source", "location",
           "company", "email", "email_source", "authority_score", "relevance_score", "score",
           "is_recommended")


def person_id(startup_id: str, key: str) -> str:
    return hashlib.sha1(f"{startup_id}|{key}".encode("utf-8")).hexdigest()[:12]


def _row_to_dict(r) -> dict:
    d = dict(r)
    d["relationship"] = json.loads(d["relationship"]) if d.get("relationship") else []
    d["is_recommended"] = bool(d.get("is_recommended"))
    return d


def get(pid: str) -> dict | None:
    r = db.conn().execute("SELECT * FROM startup_contacts WHERE id = ?", (pid,)).fetchone()
    return _row_to_dict(r) if r else None


def set_message(pid: str, msg: dict) -> dict | None:
    db.conn().execute(
        "UPDATE startup_contacts SET msg_subject=?, msg_email=?, msg_linkedin=?, msg_type=?, "
        "msg_angle=?, msg_at=? WHERE id=?",
        (msg.get("subject", ""), msg.get("email_body", ""), msg.get("linkedin_note", ""),
         msg.get("msg_type", ""), msg.get("angle", ""),
         time.strftime("%Y-%m-%dT%H:%M:%S"), pid))
    db.conn().commit()
    return get(pid)


def replace_for_startup(startup_id: str, people: list[dict]) -> int:
    """Swap in a freshly-discovered, ranked set of contacts for one company."""
    c = db.conn()
    c.execute("DELETE FROM startup_contacts WHERE startup_id = ?", (startup_id,))
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for p in people:
        pid = p.get("id") or person_id(startup_id, p.get("profile_url") or p.get("name", ""))
        vals = {k: p.get(k) for k in _FIELDS}
        vals["is_recommended"] = 1 if p.get("is_recommended") else 0
        vals.update(id=pid, startup_id=startup_id,
                    relationship=json.dumps(p.get("relationship") or []), created_at=now)
        cols = ", ".join(vals)
        c.execute(f"INSERT INTO startup_contacts ({cols}) VALUES ({', '.join(':'+k for k in vals)})",
                  vals)
    c.commit()
    return len(people)


def list_for_startup(startup_id: str) -> list[dict]:
    rows = db.conn().execute(
        "SELECT * FROM startup_contacts WHERE startup_id = ? "
        "ORDER BY score DESC, authority_score DESC", (startup_id,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def count() -> int:
    return db.conn().execute("SELECT COUNT(*) n FROM startup_contacts").fetchone()["n"]
