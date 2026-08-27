"""Big companies known for sending online assessments (OAs) early in the internship pipeline.

Used to pin ONE of these to the front of the FIRST Find+open batch each day — apply early
to an OA shop so the timer starts while you grind through the rest.
"""
import re

# Canonical display names. Matching is case-insensitive and allows common brand aliases.
# Keep this list focused on "sends an OA / Codesignal / HackerRank early" shops, not every FAANG.
OA_COMPANIES = [
    "IBM",
    "Capital One",
    "Amazon",
    "Google",
    "Meta",
    "Microsoft",
    "Apple",
    "NVIDIA",
    "Oracle",
    "Cisco",
    "Intel",
    "Salesforce",
    "Adobe",
    "Uber",
    "Lyft",
    "DoorDash",
    "Airbnb",
    "Stripe",
    "Coinbase",
    "Databricks",
    "Snowflake",
    "Palantir",
    "Bloomberg",
    "Roblox",
    "Netflix",
    "Spotify",
    "Block",
    "ServiceNow",
    "Workday",
    "Dell",
    "HP",
    "Visa",
    "Mastercard",
    "American Express",
    "PayPal",
    "Intuit",
    "Twilio",
    "Cloudflare",
    "Atlassian",
    "Shopify",
    "Zoom",
    "Dropbox",
    "Pinterest",
    "Snap",
    "LinkedIn",
    "Walmart",
    "Target",
    "Nike",
    "Disney",
    "Comcast",
    "JPMorgan",
    "Goldman Sachs",
    "Morgan Stanley",
    "Citigroup",
    "Bank of America",
    "Wells Fargo",
    "Citadel",
    "Jane Street",
    "Two Sigma",
    "Hudson River Trading",
    "Optiver",
    "IMC",
    "Jump Trading",
    "DRW",
    "Akuna Capital",
    "Susquehanna",
    "RTX",
    "Lockheed Martin",
    "Northrop Grumman",
    "Boeing",
    "Tesla",
    "General Motors",
    "Ford",
    "Accenture",
    "Deloitte",
]

# Brand / subsidiary aliases → canonical name from OA_COMPANIES.
_ALIASES = {
    "meta platforms": "Meta",
    "facebook": "Meta",
    "instagram": "Meta",
    "alphabet": "Google",
    "youtube": "Google",
    "amazon web services": "Amazon",
    "aws": "Amazon",
    "jp morgan": "JPMorgan",
    "jpmorgan chase": "JPMorgan",
    "j p morgan": "JPMorgan",
    "chase": "JPMorgan",
    "goldman sachs group": "Goldman Sachs",
    "goldman": "Goldman Sachs",
    "morgan stanley": "Morgan Stanley",
    "citi": "Citigroup",
    "citibank": "Citigroup",
    "bofa": "Bank of America",
    "bank of america": "Bank of America",
    "wells fargo": "Wells Fargo",
    "american express": "American Express",
    "amex": "American Express",
    "square": "Block",
    "block inc": "Block",
    "hewlett packard": "HP",
    "hp inc": "HP",
    "international business machines": "IBM",
    "ibm corporation": "IBM",
    "raytheon": "RTX",
    "raytheon technologies": "RTX",
    "collins aerospace": "RTX",
    "pratt & whitney": "RTX",
    "lockheed": "Lockheed Martin",
    "northrop": "Northrop Grumman",
    "hrt": "Hudson River Trading",
    "sig": "Susquehanna",
    "susquehanna international": "Susquehanna",
    "akuna": "Akuna Capital",
    "snapchat": "Snap",
    "snap inc": "Snap",
    "linkedin corporation": "LinkedIn",
    "walmart labs": "Walmart",
    "gm": "General Motors",
    "capitalone": "Capital One",
}

_GENERIC = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|group|holdings|holding|"
    r"plc|ltd|llc|the|technologies|technology|labs|systems|solutions)\b\.?",
    re.I,
)


def _norm(name: str) -> str:
    n = _GENERIC.sub(" ", (name or "").lower())
    n = re.sub(r"[^a-z0-9]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


_canon_by_norm: dict[str, str] | None = None


def _index() -> dict[str, str]:
    global _canon_by_norm
    if _canon_by_norm is None:
        _canon_by_norm = {_norm(c): c for c in OA_COMPANIES}
        for alias, canon in _ALIASES.items():
            _canon_by_norm[_norm(alias)] = canon
    return _canon_by_norm


def match(company_name: str) -> str | None:
    """Canonical OA-company name if `company_name` matches, else None."""
    idx = _index()
    key = _norm(company_name)
    if not key:
        return None
    if key in idx:
        return idx[key]
    # Prefix / containment for multi-word names ("Capital One Financial" → Capital One).
    key_tokens = key.split()
    key_set = set(key_tokens)
    for norm_name, canon in idx.items():
        name_tokens = norm_name.split()
        if not name_tokens:
            continue
        if len(name_tokens) >= 2 and set(name_tokens) <= key_set:
            return canon
        if len(name_tokens) == 1 and name_tokens[0] in key_set and len(name_tokens[0]) >= 4:
            # Single distinctive token ("NVIDIA", "Oracle", "Roblox") — avoid tiny words.
            return canon
    return None


def is_oa_company(company_name: str) -> bool:
    return match(company_name) is not None
