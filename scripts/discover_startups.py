"""Standalone startup discovery — the outreach Phase 1 pipeline without the server.

Runs sources -> rank -> (optionally) enrich the top companies' contacts, and prints a ranked list.
Handy for debugging scoring/sources; the website dashboard uses the same backend package.

Run: .venv/bin/python scripts/discover_startups.py [--limit N] [--all] [--contacts K]
  --all       include non-hiring / older-batch companies (default: recent + hiring only)
  --contacts  enrich the top K companies with a real founder + LinkedIn (default 5; 0 to skip)
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.discovery import sources, ranking, contacts  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--all", action="store_true", help="include non-hiring / older batches")
    ap.add_argument("--contacts", type=int, default=5, help="enrich top-K contacts (0 = skip)")
    args = ap.parse_args()

    prof = json.load(open(os.path.expanduser("~/ResumeRewriter/profile.json")))
    filters = {"limit": args.limit,
               "hiring_only": not args.all, "recent_only": not args.all}
    print("Discovering startups from YC" + ("" if args.all else " (recent + hiring)") + "…\n")
    companies = sources.discover(filters)
    ranking.rank(companies, prof, recent_batches=set(sources.recent_batches(1)))
    if not companies:
        print("No companies matched — try --all or check connectivity.")
        return

    for i, c in enumerate(companies[:args.limit]):
        reco = ""
        if i < args.contacts:
            e = contacts.enrich(c.get("slug", ""))
            r = contacts.recommend(c, e.get("founders", []))
            who = r["contact_name"] or r["contact_title"]
            reco = f"  → contact: {who}" + (f" ({r['contact_linkedin']})" if r["contact_linkedin"] else "")
        ts = c.get("team_size")
        print(f"[{c['fit_score']:>3} fit · {c['hiring_label']:<6}] {c['name']}"
              f"  ({ts if ts else '?'} ppl · {c.get('batch','')})")
        print(f"      {c.get('one_liner','')}")
        print(f"      fit: {c['fit_reason']}")
        print(f"      hiring: {', '.join(c['hiring_signals']) or '—'}")
        if reco:
            print(reco)
        print(f"      {c.get('source_url','')}")
        print()
    print(f"Total: {len(companies)} companies "
          f"(showed {min(len(companies), args.limit)}, enriched top {min(args.contacts, len(companies))}).")


if __name__ == "__main__":
    main()
