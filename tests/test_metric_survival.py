"""A hard metric in the evidence must reach the rendered resume exactly once.

The local model is instructed to keep real numbers and usually does, but "usually" is not good
enough for the single quantified claim on an entry: condensing a long evidence bullet drops the
trailing result clause first. _ensure_metric puts it back deterministically. The failure to guard
hardest against is the OPPOSITE one — re-adding a metric the model actually kept, in different
words, which reads as "Supported 160 students ... helping 160 students".
"""
import pytest

from backend.resume import quality
from backend.resume.builder import _ensure_metric

TA = ("Selected as an undergraduate teaching assistant for CS 303E, UT Austin's introductory "
      "Python course, helping 160 students with no prior programming experience.")
BOT = ("Shipped a Chrome extension that reads a job posting off the page, autofills applications "
       "through chrome.debugger trusted input, and one-tap attaches the resume and transcript into "
       "file inputs, cutting about 10 minutes of manual form-filling from every application.")


@pytest.mark.parametrize("evidence,number,rendered", [
    (TA, "160", "Served as a teaching assistant for CS 303E at UT Austin."),
    (BOT, "10", "Shipped a Chrome extension that automates application filling."),
])
def test_dropped_metric_is_restored(evidence, number, rendered):
    out, _ = _ensure_metric([rendered], [evidence])
    assert number in out[0]


@pytest.mark.parametrize("evidence,number,rendered", [
    # kept verbatim
    (TA, "160", "Taught CS 303E, helping 160 students with no prior programming experience."),
    # kept, but reworded so the unit no longer sits beside the digits — the double-up case
    (TA, "160", "Supported 160 introductory Python students as a TA for CS 303E."),
    (BOT, "10", "Shipped a Chrome extension that saves 10 minutes on every application."),
])
def test_kept_metric_is_never_added_twice(evidence, number, rendered):
    out, _ = _ensure_metric([rendered], [evidence])
    assert out[0].count(number) == 1


def test_lands_on_the_bullet_it_belongs_to():
    others = ["Built an embedding-based evidence ranker over the profile.",
              "Shipped a Chrome extension that automates application filling."]
    out, _ = _ensure_metric(list(others), [BOT])
    assert "10 minutes" in out[1] and "10 minutes" not in out[0]


def test_skips_rather_than_overflow_the_line_budget():
    long_bullet = ("Shipped a Chrome extension that reads a job posting off the page and autofills "
                   "every application field through trusted debugger input while attaching the "
                   "resume and transcript in one tap today.")
    out, warns = _ensure_metric([long_bullet], [BOT])
    assert out == [long_bullet]
    assert warns and "length limit" in warns[0]


def test_restored_bullet_stays_within_the_length_budget():
    out, _ = _ensure_metric(["Shipped a Chrome extension that automates application filling."], [BOT])
    assert "too long" not in quality.validate_bullet(out[0], set(), BOT)


@pytest.mark.parametrize("text", [
    "helping 160 students with no prior programming experience",
    "cutting about 10 minutes of manual form-filling from every application",
    "saving 5 minutes per run",
])
def test_time_and_people_counts_score_as_metrics(text):
    assert "metric" in quality.impact_signals(text)


# ---------------------------------------------------------------- the declared `metrics` field

BOT_METRIC = "cutting about 10 minutes of manual form-filling from every application"
TA_METRIC = "helping 160 students with no prior programming experience"


def test_declared_metric_is_enforced_even_when_absent_from_every_bullet():
    """The whole point of the field: the number lives in `metrics`, not in the evidence prose,
    so nothing but the guarantee can put it on the resume."""
    out, _ = _ensure_metric(["Shipped a Chrome extension that automates application filling."],
                            ["Shipped a Chrome extension."], [BOT_METRIC])
    assert "10 minutes" in out[0]


def test_declared_metric_not_repeated_when_the_model_already_used_it():
    out, _ = _ensure_metric(["Supported 160 introductory Python students as a TA."],
                            ["Taught CS 303E."], [TA_METRIC])
    assert out[0].count("160") == 1


def test_non_gerund_metric_is_parenthesised_so_the_sentence_stays_grammatical():
    out, _ = _ensure_metric(["Led an indoor-navigation startup."], ["Led a startup."],
                            ["100 test users onboarded"])
    assert out[0] == "Led an indoor-navigation startup (100 test users onboarded)."


def test_metric_without_a_number_is_reported_not_silently_ignored():
    out, warns = _ensure_metric(["Led a startup."], ["Led a startup."], ["lots of users"])
    assert out == ["Led a startup."]
    assert warns and "no number" in warns[0]


def test_profile_entries_expose_their_declared_metrics():
    from backend import profile_store
    from backend.resume.builder import _metrics_of
    p = profile_store.load()
    named = {(e.get("title") or e.get("name")): _metrics_of(e)
             for e in p["work_experience"] + p["projects"]}
    for entry in ("Founder and Developer", "Undergraduate Teaching Assistant",
                  "ML Racecar", "AI Resume & Outreach Agent"):
        assert named.get(entry), f"{entry} lost its metric"
