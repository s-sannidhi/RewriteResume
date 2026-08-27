#!/usr/bin/env python3
"""Turn a JD text file into a tailored resume (text layer). No server needed — calls the
backend directly.

Usage:
    .venv/bin/python scripts/make_resume.py jds/swe_intern_example.txt
    .venv/bin/python scripts/make_resume.py jds/swe_intern_example.txt "Backend reliability & APIs"
    .venv/bin/python scripts/make_resume.py jds/swe_intern_example.txt --pdf   # also render a 1-page PDF

Arg 1: path to a JD text file.
Arg 2 (optional): focus angle, OR --pdf. If a focus angle is given, --pdf can follow it.

Requires Ollama up with the model in backend/config.py (gemma3:12b) + nomic-embed-text.
Takes ~40s (two LLM calls).
"""
import sys
import time
from pathlib import Path

# Allow running as `python scripts/make_resume.py` from anywhere — put project root on path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import jd_analyzer
from backend.resume import builder


def main():
    args = [a for a in sys.argv[1:]]
    want_pdf = "--pdf" in args
    args = [a for a in args if a != "--pdf"]
    if not args:
        print("usage: make_resume.py <jd_file.txt> [focus_angle] [--pdf]")
        sys.exit(1)
    sys.argv = [sys.argv[0]] + args  # keep the rest of main() reading argv as before
    jd = open(sys.argv[1], encoding="utf-8").read()

    print(f"Analyzing JD ({len(jd)} chars)…")
    t = time.time()
    a = jd_analyzer.analyze(jd)
    print(f"  {time.time()-t:.1f}s — {a.get('company')} · {a.get('role_title')} ({a.get('seniority')})")
    print(f"  tech in JD: {', '.join(a.get('concrete_tech', []))}")
    recs = [r['term'] for r in a.get('keyword_recommendations', [])]
    print(f"  missing concrete skills you could add: {recs or '(none)'}")

    angle = sys.argv[2] if len(sys.argv) > 2 else (a.get("angle_ideas") or ["Backend"])[0]
    print(f"\nGenerating resume — focus angle: \"{angle}\"…")
    t = time.time()
    r = builder.generate_text(a, angle)
    print(f"  {time.time()-t:.1f}s\n")

    line = "─" * 64
    ident = r["identity"]
    print(line)
    print(f"  {ident.get('legal_name','')}".upper())
    print(f"  {ident.get('email','')} · {ident.get('phone','')} · {ident.get('location','')}")
    print(f"  {ident.get('linkedin','')}  {ident.get('github','')}")
    print(line)
    if r["summary"]:
        print(f"\nSUMMARY\n  {r['summary']}")

    print("\nEDUCATION")
    for e in r["education"]:
        print(f"  {e['school']} — {e['degree_line']}   {e['gpa']}")
        if e.get("coursework"):
            print(f"    Coursework: {', '.join(e['coursework'])}")

    print("\nEXPERIENCE")
    for w in r["work"]:
        print(f"  {w['title']} — {w['company']} · {w['location']}   ({w['dates']})")
        for b in w["bullets"]:
            print(f"    • {b}")

    print("\nPROJECTS")
    for p in r["projects"]:
        head = p["name"] + (f"  [{p['tech']}]" if p.get("tech") else "")
        print(f"  {head}   ({p['dates']})")
        for b in p["bullets"]:
            print(f"    • {b}")

    print("\nSKILLS")
    for group, items in r["skills"].items():
        print(f"  {group.replace('_',' ').title()}: {', '.join(items)}")
    print(f"\n{line}\n(Education shows no graduation date by design.)")

    if want_pdf:
        from backend.resume import renderer
        a["jd_text"] = jd
        print("\nRendering 1-page PDF (fit loop)…")
        t = time.time()
        res = renderer.generate_pdf(a, angle, gen=r)
        print(f"  {time.time()-t:.1f}s — scale {res['scale']}, {res['pages']} page")
        print(f"  saved: {res['pdf_path']}")


if __name__ == "__main__":
    main()
