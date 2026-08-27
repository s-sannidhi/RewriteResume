"""Regression tests for the duplicated-phrasing bug.

Root cause (fixed 2026-08-17): builder._ensure_specifics re-inserted a "protected" parenthetical
whenever an EXACT substring match for it was absent from the rewritten bullets. Any paraphrase
therefore looked like a drop, so the parenthetical was appended verbatim next to the prose that
already said it. When the anchor word was missing too, it was appended at the end of the sentence,
attaching a specific to the wrong clause.

Each case below is a real defect observed in a generated resume.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.resume import builder, quality  # noqa: E402


OBSERVED_DEFECTS = [
    pytest.param(
        ["Trained on one track and evaluated generalization on held-out circuits "
         "(~95-100% completion for the top methods)."],
        ["Trained on one track and evaluated generalization on held-out circuits, "
         "achieving ~95-100% completion for top methods."],
        id="paraphrased-parenthetical-not-reappended",
    ),
    pytest.param(
        ["Shipped a React Native wayfinding app with Firebase (Auth, Firestore, Storage), "
         "onboarding 100 test users."],
        ["Shipped a cross-platform wayfinding app, onboarding 100 test users."],
        id="no-anchor-does-not-append-to-wrong-clause",
    ),
    pytest.param(
        ["Built a map editor that turns an uploaded floor plan (any image file) into a graph.",
         "Converts an uploaded plan (any image) into a routable graph."],
        ["Converts an uploaded floor plan into a routable adjacency graph."],
        id="overlapping-parentheticals-inserted-once",
    ),
]


@pytest.mark.parametrize("evidence,rewritten", OBSERVED_DEFECTS)
def test_ensure_specifics_never_duplicates(evidence, rewritten):
    out, _warnings = builder._ensure_specifics(list(rewritten), evidence)
    for bullet in out:
        assert quality.internal_duplication(bullet) == [], bullet


def test_paraphrase_counts_as_coverage():
    """The specific bug: prose restating the parenthetical must count as already-present."""
    assert quality.paren_covered("(~95-100% completion for the top methods)",
                                 "achieving ~95-100% completion for top methods")


def test_longer_parenthetical_covers_shorter_overlapping_one():
    assert quality.paren_covered("(any image)", "converts a floor plan (any image file) to a graph")


def test_missing_anchor_warns_instead_of_misplacing():
    out, warnings = builder._ensure_specifics(
        ["Shipped a cross-platform wayfinding app, onboarding 100 test users."],
        ["Shipped a React Native app with Firebase (Auth, Firestore, Storage), onboarding 100 test users."])
    assert "(Auth, Firestore, Storage)" not in out[0], "specific attached to the wrong clause"
    assert warnings, "dropping a specific must be reported, not silent"


def test_unbalanced_paren_is_repaired():
    assert quality.fix_unbalanced_parens("runs entirely on one machine).") == \
        "runs entirely on one machine."


def test_strip_removes_redundant_parenthetical_only():
    out, removed = quality.strip_internal_duplication(
        "Achieved ~95-100% completion (~95-100% completion for the top methods).")
    assert removed and "(~95-100%" not in out
    out2, removed2 = quality.strip_internal_duplication(
        "Converts a floor plan (any image file) into a routable graph.")
    assert removed2 == [] and "(any image file)" in out2, "a genuine specific must survive"


def test_no_internal_duplication_after_polish():
    """End-to-end through builder._polish (the pre-render gate) — referenced by
    quality.VERIFIED_CLAIMS as the proof for the no-duplicated-phrasing property."""
    gen = {
        "work": [{"id": "w1", "bullets": [
            "Shipped an app on Firebase (Auth, Firestore, Storage) using Firebase Auth, Firestore and Storage.",
            "Achieved ~95-100% completion (~95-100% completion for the top methods).",
        ]}],
        "projects": [{"id": "p1", "bullets": [
            "Converts an uploaded floor plan (any image file) (any image) into a graph.",
        ]}],
        "skills": {},
    }
    out = builder._polish(gen, {"role_title": "SWE", "concrete_tech": []}, {"work_experience": [],
                                                                           "projects": []})
    for section in ("work", "projects"):
        for e in out[section]:
            for b in e["bullets"]:
                assert quality.internal_duplication(b) == [], b
    assert out["duplication_removed"], "the polish pass should record what it removed"
