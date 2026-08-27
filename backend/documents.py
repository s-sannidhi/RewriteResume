"""Frequently-uploaded documents — the ones you attach during job applications, surfaced so the
extension can drop them straight into an upload field (no File Explorer hunt).

Two sources:
  - Static docs you keep in ~/ResumeRewriter/documents/ (transcript, course schedule, anything you
    upload a lot). Just drop files in that folder; they show up here.
  - Generated resumes + cover letters, read from the tracker.

Every item carries an ABSOLUTE path. The extension attaches it via chrome.debugger
(DOM.setFileInputFiles) or, as a fallback, copies the path for the macOS Open dialog. `resolve()`
guards every path so only files under ~/ResumeRewriter/ are ever served, revealed, or opened.
"""
import os
import subprocess
import sys
from pathlib import Path

from . import config
from .store import tracker

_EXTS = {".pdf", ".doc", ".docx", ".txt", ".rtf", ".png", ".jpg", ".jpeg"}


def _kind_of(name: str) -> str:
    n = name.lower()
    if "transcript" in n or "academic" in n:
        return "transcript"
    if "schedule" in n or "courses" in n:
        return "schedule"
    return "doc"


def _static_docs() -> list[dict]:
    out = []
    for p in sorted(config.DOCUMENTS_DIR.glob("*")):
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in _EXTS:
            out.append({
                "name": p.stem.replace("_", " ").strip() or p.name,
                "filename": p.name,
                "kind": _kind_of(p.name),
                "ext": p.suffix.lower().lstrip("."),
                "path": str(p),
                "mtime": p.stat().st_mtime,
            })
    return out


def _tracker_docs() -> tuple[list[dict], list[dict]]:
    """(resumes, cover_letters) from the tracker — most recent first, only files that exist."""
    resumes, covers = [], []
    for r in tracker.list_all(limit=500):
        folder = (r.get("folder") or "").strip()
        if not folder:
            continue
        base = config.RESUMES_DIR / folder.split("/")[-1]
        label = " — ".join(x for x in [r.get("company") or "", r.get("role") or ""] if x) or "resume"
        rp = base / (r.get("pdf_filename") or "resume.pdf")
        if rp.exists():
            resumes.append({
                "name": label, "filename": rp.name, "kind": "resume", "ext": "pdf",
                "path": str(rp), "company": r.get("company") or "", "role": r.get("role") or "",
                "date": (r.get("created_at") or "")[:10], "tracker_id": r.get("id"),
            })
        cf = r.get("cover_letter_filename")
        if cf:
            cp = base / cf
            if cp.exists():
                covers.append({
                    "name": label, "filename": cp.name, "kind": "cover", "ext": "pdf",
                    "path": str(cp), "company": r.get("company") or "", "role": r.get("role") or "",
                    "date": (r.get("created_at") or "")[:10], "tracker_id": r.get("id"),
                })
    return resumes, covers


def listing() -> dict:
    resumes, covers = _tracker_docs()
    return {
        "frequent": _static_docs(),
        "resumes": resumes,
        "cover_letters": covers,
        "documents_dir": str(config.DOCUMENTS_DIR),
    }


def resolve(path: str) -> Path | None:
    """Absolute path, but ONLY if it lives under ~/ResumeRewriter/ — blocks traversal / arbitrary
    file access even though this is a local single-user server."""
    if not path:
        return None
    try:
        p = Path(path).expanduser().resolve()
        root = config.DATA_DIR.resolve()
        if root == p or root in p.parents:
            return p if p.exists() else None
    except Exception:
        return None
    return None


def reveal(path: str) -> bool:
    """Show a file in the OS file manager, selected: Finder (macOS), Explorer (Windows), or the
    desktop default (Linux, which has no standard 'select this file' verb, so we open the parent
    folder). Path must be under DATA_DIR."""
    p = resolve(path)
    if not p:
        return False
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", str(p)], check=False)
        elif os.name == "nt":
            # /select, needs the path as one argument and tolerates explorer's nonzero exit code.
            subprocess.run(["explorer", f"/select,{p}"], check=False)
        else:
            subprocess.run(["xdg-open", str(p.parent)], check=False)
        return True
    except Exception:
        return False


def open_folder(path: str | None = None) -> bool:
    """Open a folder (default: the documents dir) in the OS file manager."""
    target = resolve(path) if path else config.DOCUMENTS_DIR
    if not target:
        return False
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=False)
        elif os.name == "nt":
            os.startfile(str(target))        # type: ignore[attr-defined]  # Windows-only
        else:
            subprocess.run(["xdg-open", str(target)], check=False)
        return True
    except Exception:
        return False
