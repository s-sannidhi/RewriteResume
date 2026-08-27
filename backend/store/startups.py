"""Discovered-startups store (outreach Phase 1). Mirrors tracker.py: one SQLite table, single-user.

A discovery run upserts by id (stable sha1 of the domain): scores + freshness fields refresh, but
a user-set `status` (saved/skipped/contacted) and any enriched contact are preserved. The dashboard
reads list_all(); "Find contact" writes via set_contact().
"""
import json
import time
from . import db

STATUSES = ("new", "saved", "skipped", "contacted")

# Columns carried straight from a normalized+ranked company dict into the row.
_FIELDS = ("name", "website", "domain", "one_liner", "description", "team_size", "stage", "batch",
           "industry", "subindustry", "location", "is_hiring", "source", "source_url",
           "fit_score", "fit_reason", "hiring_score", "hiring_label")
_JSON_FIELDS = ("tags", "regions", "hiring_signals", "raw")


def _row_to_dict(r) -> dict:
    d = dict(r)
    for k in _JSON_FIELDS:
        if k in d:
            d[k] = json.loads(d[k]) if d.get(k) else ([] if k != "raw" else {})
    if "intel" in d:
        d["intel"] = json.loads(d["intel"]) if d.get("intel") else None
    d["is_hiring"] = bool(d.get("is_hiring"))
    return d


def upsert_many(companies: list[dict]) -> int:
    """Insert new companies / refresh scores of known ones. Preserves user status + contact."""
    c = db.conn()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    n = 0
    for co in companies:
        cid = co.get("id")
        if not cid:
            continue
        existing = c.execute("SELECT status FROM startups WHERE id = ?", (cid,)).fetchone()
        vals = {f: co.get(f) for f in _FIELDS}
        vals["is_hiring"] = 1 if co.get("is_hiring") else 0
        for k in _JSON_FIELDS:
            vals[k] = json.dumps(co.get(k) or ([] if k != "raw" else {}))
        vals["last_seen"] = now
        if existing:
            sets = ", ".join(f"{k} = :{k}" for k in vals)
            c.execute(f"UPDATE startups SET {sets} WHERE id = :id", {**vals, "id": cid})
        else:
            vals.update(id=cid, status="new", discovered_at=now,
                        contact_name="", contact_title="", contact_linkedin="",
                        contact_role_reco="", contact_search_url="")
            cols = ", ".join(vals)
            c.execute(f"INSERT INTO startups ({cols}) VALUES ({', '.join(':' + k for k in vals)})",
                      vals)
        n += 1
    c.commit()
    return n


def list_all(*, status: str | None = None, min_fit: int = 0, hiring_only: bool = False,
             limit: int = 300) -> list[dict]:
    q = "SELECT * FROM startups WHERE fit_score >= ?"
    args: list = [min_fit]
    if status:
        q += " AND status = ?"; args.append(status)
    if hiring_only:
        q += " AND is_hiring = 1"
    q += " ORDER BY fit_score DESC, hiring_score DESC LIMIT ?"
    args.append(limit)
    return [_row_to_dict(r) for r in db.conn().execute(q, args).fetchall()]


def get(cid: str) -> dict | None:
    r = db.conn().execute("SELECT * FROM startups WHERE id = ?", (cid,)).fetchone()
    return _row_to_dict(r) if r else None


def set_contact(cid: str, contact: dict) -> dict | None:
    db.conn().execute(
        "UPDATE startups SET contact_name=?, contact_title=?, contact_linkedin=?, "
        "contact_role_reco=?, contact_search_url=? WHERE id=?",
        (contact.get("contact_name", ""), contact.get("contact_title", ""),
         contact.get("contact_linkedin", ""), contact.get("contact_role_reco", ""),
         contact.get("contact_search_url", ""), cid))
    db.conn().commit()
    return get(cid)


def set_intel(cid: str, intel: dict) -> dict | None:
    db.conn().execute("UPDATE startups SET intel = ?, intel_at = ? WHERE id = ?",
                      (json.dumps(intel), time.strftime("%Y-%m-%dT%H:%M:%S"), cid))
    db.conn().commit()
    return get(cid)


def set_status(cid: str, status: str) -> dict | None:
    if status not in STATUSES:
        return None
    db.conn().execute("UPDATE startups SET status = ? WHERE id = ?", (status, cid))
    db.conn().commit()
    return get(cid)


def count() -> int:
    return db.conn().execute("SELECT COUNT(*) n FROM startups").fetchone()["n"]
