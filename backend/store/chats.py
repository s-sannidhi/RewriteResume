"""Persistent chat threads for the website AI Ask mode. Single-user, one JSON file at
~/ResumeRewriter/chats.json. Each thread keeps its full message history forever."""
import json
import time
import uuid

from .. import config

_PATH = config.DATA_DIR / "chats.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _load() -> dict:
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and "threads" in d else {"threads": []}
    except Exception:
        return {"threads": []}


def _save(d: dict) -> None:
    _PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")


def list_threads() -> list[dict]:
    d = _load()
    out = [{"id": t["id"], "title": t.get("title") or "New chat",
            "updated_at": t.get("updated_at", ""), "count": len(t.get("messages", []))}
           for t in d["threads"]]
    out.sort(key=lambda t: t["updated_at"], reverse=True)
    return out


def get_thread(tid: str) -> dict | None:
    return next((t for t in _load()["threads"] if t["id"] == tid), None)


def create_thread(title: str = "") -> dict:
    d = _load()
    t = {"id": uuid.uuid4().hex[:12], "title": title or "New chat",
         "created_at": _now(), "updated_at": _now(), "messages": []}
    d["threads"].append(t)
    _save(d)
    return t


def add_message(tid: str, role: str, content: str) -> dict | None:
    d = _load()
    t = next((x for x in d["threads"] if x["id"] == tid), None)
    if not t:
        return None
    t.setdefault("messages", []).append({"role": role, "content": content, "at": _now()})
    t["updated_at"] = _now()
    if role == "user" and (t.get("title") in ("", "New chat")):
        t["title"] = " ".join(content.split())[:48]   # auto-title from the first message
    _save(d)
    return t


def delete_thread(tid: str) -> None:
    d = _load()
    d["threads"] = [t for t in d["threads"] if t["id"] != tid]
    _save(d)
