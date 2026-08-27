#!/usr/bin/env python3
"""Phase-4 demo: feed a simulated application page (mixed widgets) through the autofill brain
and print the resolved action for each field. No extension, no server needed.

Usage: .venv/bin/python scripts/autofill_demo.py
"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.autofill import resolver

# A realistic mixed page — text inputs, selects, radios, a textarea essay, a file upload.
PAGE = [
    {"id": "f1", "label": "First Name", "name": "firstName", "type": "text", "autocomplete": "given-name"},
    {"id": "f2", "label": "Last Name", "name": "lastName", "type": "text"},
    {"id": "f3", "label": "Email Address", "name": "email", "type": "email"},
    {"id": "f4", "label": "Phone", "name": "phone", "type": "tel"},
    {"id": "f5", "label": "Postal Code", "name": "zip", "type": "text"},
    {"id": "f6", "label": "City", "name": "city", "type": "text"},
    {"id": "f7", "label": "State", "name": "state", "type": "text"},
    {"id": "f8", "label": "LinkedIn Profile", "name": "linkedin", "type": "url"},
    {"id": "f9", "label": "GitHub", "name": "github", "type": "url"},
    {"id": "f10", "label": "Are you legally authorized to work in the United States?",
     "name": "workAuth", "type": "radio", "options": ["Yes", "No"]},
    {"id": "f11", "label": "Will you now or in the future require sponsorship for employment visa status?",
     "name": "sponsor", "type": "radio", "options": ["Yes", "No"]},
    {"id": "f12", "label": "Gender", "name": "gender", "type": "select",
     "options": ["Male", "Female", "Non-binary", "Prefer not to say"]},
    {"id": "f13", "label": "Please select your Veterans Status", "name": "vet", "type": "select",
     "options": ["I am a protected veteran", "I am not a protected veteran", "I prefer not to answer"]},
    {"id": "f14", "label": "Disability Status", "name": "disability", "type": "select",
     "options": ["Yes, I have a disability", "No, I do not have a disability", "I prefer not to answer"]},
    {"id": "f15", "label": "School", "name": "school", "type": "text"},
    {"id": "f16", "label": "Degree", "name": "degree", "type": "text"},
    {"id": "f17", "label": "Major / Field of Study", "name": "major", "type": "text"},
    {"id": "f18", "label": "GPA", "name": "gpa", "type": "text"},
    {"id": "f19", "label": "Expected Graduation Date", "name": "gradDate", "type": "text"},
    {"id": "f20", "label": "Desired Salary", "name": "salary", "type": "text"},
    {"id": "f21", "label": "Earliest Start Date", "name": "startDate", "type": "text"},
    {"id": "f22", "label": "Are you willing to relocate?", "name": "relocate", "type": "radio",
     "options": ["Yes", "No"]},
    {"id": "f23", "label": "How did you hear about us?", "name": "source", "type": "text"},
    {"id": "f24", "label": "Have you ever been convicted of a felony?", "name": "felony",
     "type": "radio", "options": ["Yes", "No"]},
    {"id": "f25", "label": "Do you consent to a background check?", "name": "bgcheck",
     "type": "radio", "options": ["Yes", "No"]},
    {"id": "f26", "label": "Resume / CV", "name": "resume", "type": "file"},
    {"id": "f27", "label": "Why do you want to work at Vantage Labs?", "name": "whyUs",
     "type": "textarea"},
]

JD = {"company": "Vantage Labs", "role_title": "Software Engineer Intern",
      "summary": "Backend intern building REST APIs in Python/Node with PostgreSQL on AWS."}


def main():
    t = time.time()
    actions = resolver.answer_page(PAGE, jd=JD)
    dt = time.time() - t

    by_label = {f["id"]: f["label"] for f in PAGE}
    src = {}
    review = 0
    for a in actions:
        src[a["source"]] = src.get(a["source"], 0) + 1
        review += a["needs_review"]
        shown = a["option"] or a["value"]
        shown = (shown[:54] + "…") if shown and len(shown) > 55 else shown
        flag = "  ⚠review" if a["needs_review"] else ""
        print(f"  {a['action']:14} {a['source']:11} c={a['confidence']:.2f}  "
              f"[{a['field_kind']}] {by_label[a['field_id']][:40]:40} -> {shown!r}{flag}")

    print(f"\n  {len(actions)} fields in {dt:.1f}s | by source: {src} | needs review: {review}")
    instant = sum(v for k, v in src.items() if k in ("profile_map", "qa_memory"))
    print(f"  instant (no-LLM) coverage: {instant}/{len(actions)} = {100*instant//len(actions)}%")


if __name__ == "__main__":
    main()
