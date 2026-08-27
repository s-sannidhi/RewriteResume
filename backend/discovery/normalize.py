"""Company normalization: a stable id from the domain, and de-dup/merge across sources.

Every source emits the same normalized shape (see sources.py docstring). Dedup is by domain so
that when a second source (GitHub, etc.) is added later, the same company merges into one row
instead of appearing twice.
"""
import hashlib
import re
from urllib.parse import urlparse


def domain_of(website: str) -> str:
    """Bare registrable-ish domain: lowercased host, no scheme/www/path/port. '' if unparseable."""
    if not website:
        return ""
    w = website.strip()
    if "//" not in w:
        w = "//" + w                      # let urlparse find the host when scheme is missing
    host = (urlparse(w).hostname or "").lower()
    host = re.sub(r"^www\.", "", host)
    return host


def company_id(domain: str, name: str = "") -> str:
    """Stable 12-hex id. Prefer domain (dedupes across sources); fall back to the name so a
    company with no website still gets a consistent id."""
    key = domain or ("name:" + re.sub(r"\s+", " ", name.strip().lower()))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _merge_one(a: dict, b: dict) -> dict:
    """Merge b into a. a wins on scalars it already has; list fields union; sources concat."""
    out = dict(a)
    for k, v in b.items():
        if k in ("tags", "regions", "hiring_signals"):
            merged = list(out.get(k) or [])
            for x in (v or []):
                if x not in merged:
                    merged.append(x)
            out[k] = merged
        elif k == "source":
            srcs = out.get("source", "")
            out["source"] = srcs if v in srcs.split(", ") else (f"{srcs}, {v}" if srcs else v)
        elif not out.get(k) and v:
            out[k] = v
    return out


def merge(rows: list[dict]) -> list[dict]:
    """Collapse rows that share an id (same domain) into one, unioning list-ish fields."""
    by_id: dict[str, dict] = {}
    for r in rows:
        dom = r.get("domain") or domain_of(r.get("website", ""))
        r = {**r, "domain": dom, "id": r.get("id") or company_id(dom, r.get("name", ""))}
        cid = r["id"]
        by_id[cid] = _merge_one(by_id[cid], r) if cid in by_id else r
    return list(by_id.values())
