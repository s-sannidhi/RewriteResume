"""Match internship postings against the S&P 500 (a free, complete, non-copyrighted stand-in for
"top 500 companies" — Wikipedia's Fortune 500 article deliberately omits the full list since Fortune
claims copyright over it; the S&P 500 constituent table is openly published). Data pulled once from
Wikipedia's "List of S&P 500 companies" and frozen in sp500.json — refresh manually if it goes stale
(the index changes a handful of times a year).
"""
import json
import re
from pathlib import Path

_DATA_PATH = Path(__file__).parent / "sp500.json"
_GENERIC = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|group|holdings|holding|"
                      r"plc|ltd|llc|the|class\s?[ab])\b\.?", re.I)
# Tokens a posting may append to a company name without changing WHICH company it is.
_FILLER = {"com", "services", "service", "technologies", "technology", "labs", "systems",
           "solutions", "global", "international", "usa", "us", "america", "north", "digital",
           "enterprises", "worldwide", "na"}

# Postings commonly use a different brand name than the S&P's legal/parent entity. Small, hand-kept
# list of the mismatches an internship-seeker would actually run into.
_ALIASES = {
    "google": "alphabet", "youtube": "alphabet", "google cloud": "alphabet",
    "meta": "meta platforms", "facebook": "meta platforms", "instagram": "meta platforms",
    "whatsapp": "meta platforms",
    "amazon web services": "amazon", "aws": "amazon",
    "jp morgan": "jpmorgan chase", "jpmorgan": "jpmorgan chase", "chase": "jpmorgan chase",
    "goldman sachs": "goldman sachs group",
}

_cache: list[dict] | None = None


def _norm(name: str) -> str:
    n = _GENERIC.sub(" ", (name or "").lower())
    # punctuation becomes a separator, not nothing, so "Amazon.com" tokenizes to {amazon, com}
    # and still contains the S&P entry's {amazon}.
    n = re.sub(r"[^a-z0-9]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def load() -> list[dict]:
    global _cache
    if _cache is None:
        _cache = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    return _cache


def _index() -> dict[str, str]:
    """normalized name -> canonical S&P display name."""
    return {_norm(c["name"]): c["name"] for c in load()}


_idx_cache: dict[str, str] | None = None


def match(company_name: str) -> str | None:
    """Canonical S&P 500 name if `company_name` is (a recognizable form of) one of them, else None."""
    global _idx_cache
    if _idx_cache is None:
        _idx_cache = _index()
    key = _norm(company_name)
    alias = _ALIASES.get(key)
    if alias:
        key = _norm(alias)
    if key in _idx_cache:
        return _idx_cache[key]

    key_tokens = key.split()
    key_set = set(key_tokens)
    for norm_name, canon in _idx_cache.items():
        name_tokens = norm_name.split()
        if not name_tokens:
            continue
        if len(name_tokens) > 1:
            # >=2 distinctive tokens is specific enough that a plain subset test is safe.
            if set(name_tokens) <= key_set:
                return canon
            continue
        # A single-token entry is risky: stripping generic words can reduce a name to one common
        # word ("Southern Company" -> "southern"), which would then match any company containing
        # it (e.g. "Southern New Hampshire University"). So anchor it: the token must appear, and
        # everything AFTER it must be corporate filler. A brand prefix before it is fine, which is
        # what makes "John Deere" -> "Deere & Company" and "Amazon.com Services" -> "Amazon" work
        # while rejecting the university.
        tok = name_tokens[0]
        if tok in key_tokens and all(t in _FILLER for t in key_tokens[key_tokens.index(tok) + 1:]):
            return canon
    return None
