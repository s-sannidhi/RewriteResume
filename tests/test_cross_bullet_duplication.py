"""Regression tests for duplication ACROSS bullets in one entry.

The within-a-bullet fix did not cover this case: the model rewrites ONE source fact into two
bullets ("React Native app on Firebase delivering real-time multi-floor navigation" twice) and
drops another source fact entirely. Each bullet is individually valid and they share few literal
words, so neither the substring check nor a naive word-overlap test caught it.

The two EduPath bullets below are the real defect, taken from a generated resume.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.resume import builder, quality  # noqa: E402


EDUPATH_SOURCES = [
    "Founded an indoor-navigation startup and shipped a campus wayfinding app contracted by "
    "Allen High School, onboarding 100 test users.",
    "Built the cross-platform React Native app on a Firebase backend (Auth, Firestore, Storage), "
    "delivering real-time multi-floor turn-by-turn navigation to students.",
    "Hand-mapped the school's building floor plans in QGIS into a Dijkstra/A* routing graph for "
    "multi-floor guidance.",
    "Led the company end-to-end: closed the Allen HS contract, managed funding and a small team, "
    "and delivered on schedule.",
]

# Both of these are rewrites of EDUPATH_SOURCES[1].
OBSERVED_DUPLICATE_PAIR = [
    "Delivered real-time, multi-floor navigation to students using a React Native app built on "
    "Firebase (Auth, Firestore, Storage), enabling efficient wayfinding across campus.",
    "Developed a cross-platform mobile app with React Native, enabling real-time multi-floor "
    "turn-by-turn navigation for students (Auth, Firestore, Storage).",
]


def test_the_observed_pair_is_detected_as_redundant():
    pairs = quality.redundant_pairs(OBSERVED_DUPLICATE_PAIR)
    assert pairs, "the two EduPath bullets must be seen as covering the same ground"


def test_second_bullet_adds_no_new_content():
    """The concrete definition of the defect: bullet 2 introduces no tech/metric/feature that
    bullet 1 did not already state."""
    assert 1 in quality.bullets_adding_nothing(OBSERVED_DUPLICATE_PAIR)


def test_near_paraphrase_is_caught_without_shared_wording():
    """Point 3 of the report: catch paraphrase, not just shared text."""
    a = "Built a cross-platform mobile app in React Native for campus wayfinding."
    b = "Developed a React Native application that helps students navigate the campus."
    assert quality.bullet_overlap(a, b) > 0.0
    assert 1 in quality.bullets_adding_nothing([a, b])


def test_distinct_bullets_are_not_flagged():
    """The desired end state: each bullet covers different ground."""
    good = [
        "Founded the startup and shipped a wayfinding app contracted by Allen High School, "
        "onboarding 100 test users.",
        "Built the React Native app on Firebase (Auth, Firestore, Storage) for real-time "
        "multi-floor navigation.",
        "Hand-mapped floor plans in QGIS into a Dijkstra/A* routing graph.",
        "Led the company end-to-end: closed the contract, managed funding and a small team.",
    ]
    assert quality.redundant_pairs(good) == []
    assert quality.bullets_adding_nothing(good) == []


def test_repair_swaps_a_duplicate_for_an_uncovered_source_fact():
    """The repair must ADVANCE to an uncovered fact rather than merely deleting the duplicate —
    this is what turns 'two bullets about the app' into 'one on the app, one on the routing'."""
    fixed, notes = builder._dedupe_against_sources(list(OBSERVED_DUPLICATE_PAIR), EDUPATH_SOURCES)
    assert notes, "the repair should report what it changed"
    assert quality.redundant_pairs(fixed) == [], fixed
    # something other than the Firebase/React-Native fact must now be represented
    blob = " ".join(fixed).lower()
    assert any(k in blob for k in ("qgis", "dijkstra", "allen", "founded", "led")), fixed


def test_repair_is_noop_when_bullets_are_already_distinct():
    good = [EDUPATH_SOURCES[1], EDUPATH_SOURCES[2], EDUPATH_SOURCES[3]]
    fixed, notes = builder._dedupe_against_sources(list(good), EDUPATH_SOURCES)
    assert fixed == good and notes == []
