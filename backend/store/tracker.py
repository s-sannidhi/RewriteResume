"""Minimal job-application tracker. Each finalized resume is saved with company, role, JD,
focus angle, date, and PDF path. The sidebar lists past applications.
"""
import json
import shutil
import time
import uuid
from . import db
from .. import config


STATUSES = ("generated", "saved", "sent", "applied", "interview",
            "offer", "accepted", "rejected", "withdrawn")


def list_all(limit: int = 200) -> list[dict]:
    rows = db.conn().execute(
        "SELECT id,company,role,focus_angle,status,created_at,pdf_filename,folder,"
        "cover_letter_filename,sent_at,ask_history"
        " FROM tracker ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ask_history"] = json.loads(d["ask_history"]) if d.get("ask_history") else []
        out.append(d)
    return out


def get(app_id: str) -> dict | None:
    r = db.conn().execute("SELECT * FROM tracker WHERE id = ?", (app_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("content", "jd_analysis"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    d["ask_history"] = json.loads(d["ask_history"]) if d.get("ask_history") else []
    return d


def append_ask(app_id: str, question: str, answer: str) -> list | None:
    """Attach an Ask-tab Q&A to this application's saved history. Returns the full history."""
    r = db.conn().execute("SELECT ask_history FROM tracker WHERE id = ?", (app_id,)).fetchone()
    if not r:
        return None
    hist = json.loads(r["ask_history"]) if r["ask_history"] else []
    hist.append({"question": question, "answer": answer,
                 "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    db.conn().execute("UPDATE tracker SET ask_history = ? WHERE id = ?",
                      (json.dumps(hist), app_id))
    db.conn().commit()
    return hist


def add(*, company="", role="", jd_text="", focus_angle="", folder="", pdf_filename="",
        cover_letter_filename="", status="generated", content=None, jd_analysis=None,
        jd_fingerprint="") -> str:
    app_id = uuid.uuid4().hex[:12]
    db.conn().execute(
        "INSERT INTO tracker(id,company,role,jd_text,focus_angle,folder,pdf_filename,"
        "cover_letter_filename,status,created_at,content,jd_analysis,jd_fingerprint)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (app_id, company, role, jd_text, focus_angle, folder, pdf_filename,
         cover_letter_filename, status, time.strftime("%Y-%m-%dT%H:%M:%S"),
         json.dumps(content), json.dumps(jd_analysis), jd_fingerprint),
    )
    db.conn().commit()
    return app_id


def set_status(app_id: str, status: str) -> dict | None:
    """Set status. 'sent'/'applied' stamp sent_at (used as the applied date); later
    lifecycle changes (interview/offer/etc.) keep whatever sent_at is already there."""
    if status in ("sent", "applied"):
        db.conn().execute(
            "UPDATE tracker SET status = ?, sent_at = COALESCE(sent_at, ?) WHERE id = ?",
            (status, time.strftime("%Y-%m-%dT%H:%M:%S"), app_id))
    else:
        db.conn().execute("UPDATE tracker SET status = ? WHERE id = ?", (status, app_id))
    db.conn().commit()
    return get(app_id)


def set_cover_letter(app_id: str, filename: str) -> None:
    db.conn().execute("UPDATE tracker SET cover_letter_filename = ? WHERE id = ?",
                      (filename, app_id))
    db.conn().commit()


def find_by_fingerprint(fp: str) -> dict | None:
    """Most recent tracker row with this JD fingerprint (same posting -> same row/folder)."""
    if not fp:
        return None
    r = db.conn().execute(
        "SELECT * FROM tracker WHERE jd_fingerprint = ? ORDER BY created_at DESC LIMIT 1", (fp,)
    ).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("content", "jd_analysis"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d


def update_generation(app_id: str, *, company="", role="", jd_text="", focus_angle="",
                      folder="", pdf_filename="", status="generated",
                      content=None, jd_analysis=None) -> None:
    """Refresh a row on regeneration of the SAME job (same fingerprint, same folder)."""
    db.conn().execute(
        "UPDATE tracker SET company=?,role=?,jd_text=?,focus_angle=?,folder=?,pdf_filename=?,"
        "status=?,content=?,jd_analysis=?,created_at=? WHERE id=?",
        (company, role, jd_text, focus_angle, folder, pdf_filename, status,
         json.dumps(content), json.dumps(jd_analysis),
         time.strftime("%Y-%m-%dT%H:%M:%S"), app_id),
    )
    db.conn().commit()


def delete(app_id: str) -> None:
    """Remove a tracker row AND its generated files (resume + cover letter folder)."""
    rec = get(app_id)
    if rec and rec.get("folder"):
        folder = config.RESUMES_DIR / rec["folder"].split("/")[-1]
        if folder.exists() and folder.parent == config.RESUMES_DIR:   # never escape RESUMES_DIR
            shutil.rmtree(folder, ignore_errors=True)
    db.conn().execute("DELETE FROM tracker WHERE id = ?", (app_id,))
    db.conn().commit()


def count() -> int:
    return db.conn().execute("SELECT COUNT(*) n FROM tracker").fetchone()["n"]
