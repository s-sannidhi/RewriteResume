#!/usr/bin/env python3
"""Local smoke test for the v2 backend. Exercises every Phase 1 + Phase 3 endpoint.

Usage:
    .venv/bin/python scripts/smoke.py                 # uses a built-in sample JD
    .venv/bin/python scripts/smoke.py path/to/jd.txt  # uses your own JD text file

Requires the server running (./run.sh) and Ollama up with the model in backend/config.py
(gemma3:12b) + nomic-embed-text.
"""
import sys
import time
import json
import httpx

BASE = "http://127.0.0.1:8765"

SAMPLE_JD = """Software Engineer Intern - Backend
Build REST APIs in Python and Node.js with PostgreSQL and Docker on AWS. Ship features,
write tests, collaborate on a React frontend. Kubernetes and CI/CD a plus. Strong data
structures and algorithms."""


def main():
    jd = open(sys.argv[1]).read() if len(sys.argv) > 1 else SAMPLE_JD
    c = httpx.Client(base_url=BASE, timeout=300)

    print("1) /health")
    print("  ", c.get("/health").json(), "\n")

    print("2) /profile (identity only)")
    p = c.get("/profile").json()
    print("  ", p["identity"]["legal_name"], "|",
          len(p["work_experience"]), "jobs,", len(p["projects"]), "projects\n")

    print("3) /jd/analyze   (LLM — ~15-20s)")
    t = time.time()
    a = c.post("/jd/analyze", json={"jd_text": jd}).json()
    print(f"   {time.time()-t:.1f}s  role={a.get('role_title')}  seniority={a.get('seniority')}")
    print("   concrete_tech:", a.get("concrete_tech"))
    print("   keyword recs :", [r["term"] for r in a.get("keyword_recommendations", [])])
    print("   angle ideas  :", a.get("angle_ideas"), "\n")

    angle = (a.get("angle_ideas") or ["Backend"])[0]
    print(f"4) /resume/generate-text   angle='{angle}'  (LLM — ~15-25s)")
    t = time.time()
    r = c.post("/resume/generate-text", json={"jd_analysis": a, "focus_angle": angle}).json()
    print(f"   {time.time()-t:.1f}s")
    print("   summary:", r["summary"])
    print("   skills :", r["skills"])
    for w in r["work"]:
        print(f"   • {w['title']} @ {w['company']}  ({w['dates']})")
        for b in w["bullets"]:
            print("       -", b)
    for pr in r["projects"]:
        print(f"   • {pr['name']}  ({pr['dates']})")
        for b in pr["bullets"]:
            print("       -", b)
    print("   education:", [(e["degree_line"], e["gpa"]) for e in r["education"]], "(no grad date)\n")

    print("5) /resume/feedback   ('make the first bullet more quantified')")
    t = time.time()
    r2 = c.post("/resume/feedback", json={
        "jd_analysis": a, "focus_angle": angle,
        "feedback": "Make the bullets more quantified and concrete."}).json()
    print(f"   {time.time()-t:.1f}s  first work bullet now:")
    if r2["work"]:
        print("       -", r2["work"][0]["bullets"][0], "\n")

    print("6) /qa/recall + /qa/save   (Q&A semantic memory)")
    c.post("/qa/save", json={
        "question": "Why do you want to work at our company?",
        "answer": "I admire your engineering culture and want to build reliable backend systems.",
        "field_type": "textarea"})
    m = c.post("/qa/recall", json={"question": "What attracts you to this company?"}).json()["match"]
    print("   paraphrase recall:", "HIT" if m else "MISS",
          f"(sim {m['similarity']})" if m else "")
    m2 = c.post("/qa/recall", json={"question": "What is your expected salary?"}).json()["match"]
    print("   unrelated recall :", "HIT" if m2 else "MISS (correct)")
    # clean up the demo Q&A so we don't pollute real memory
    print("   (note: this added a demo Q&A row — delete via DB if undesired)\n")

    print("7) /tracker")
    tk = c.get("/tracker").json()
    print(f"   {len(tk)} saved applications. Most recent:")
    for x in tk[:3]:
        print(f"     - {x['company']} | {x['role']}")

    print("\nSMOKE TEST OK")


if __name__ == "__main__":
    main()
