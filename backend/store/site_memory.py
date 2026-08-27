"""Autofill site memory. Per-host knowledge the extension accumulates (detected ATS, working
selectors, quirks/tips). Lets repeat applications on the same platform fill near-instantly.
Used by the Phase 4/5 autofill engine.
"""
import json
import time
import uuid
from urllib.parse import urlparse
from . import db


def _site_key(hostname: str) -> str:
    # Collapse subdomains to the registrable-ish host so myco.wd1.myworkdayjobs.com etc. share.
    parts = (hostname or "").lower().split(".")
    return ".".join(parts[-3:]) if len(parts) >= 3 else hostname.lower()


def get_for_url(url: str) -> dict | None:
    host = urlparse(url).hostname or ""
    key = _site_key(host)
    r = db.conn().execute(
        "SELECT * FROM site_memory WHERE site_key = ? ORDER BY success_count DESC LIMIT 1", (key,)
    ).fetchone()
    if not r:
        return None
    d = dict(r)
    d["tips"] = json.loads(d.get("tips") or "[]")
    d["selectors"] = json.loads(d.get("selectors") or "{}")
    return d


def upsert(url: str, *, ats="", display_name="", tips=None, selectors=None, notes="") -> str:
    host = urlparse(url).hostname or ""
    key = _site_key(host)
    c = db.conn()
    existing = c.execute("SELECT id FROM site_memory WHERE site_key = ?", (key,)).fetchone()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if existing:
        c.execute(
            "UPDATE site_memory SET ats=?, display_name=?, tips=?, selectors=?, notes=?,"
            " success_count = success_count + 1, updated_at=? WHERE id=?",
            (ats, display_name, json.dumps(tips or []), json.dumps(selectors or {}), notes,
             now, existing["id"]),
        )
        c.commit()
        return existing["id"]
    sid = uuid.uuid4().hex[:12]
    c.execute(
        "INSERT INTO site_memory(id,site_key,hostname,ats,display_name,tips,selectors,notes,"
        "success_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
        (sid, key, host, ats, display_name, json.dumps(tips or []),
         json.dumps(selectors or {}), notes, now, now),
    )
    c.commit()
    return sid
