"""Audit + regression tests for the anti-fabrication logic.

These exist because a generated resume bullet claimed this pipeline "tailors resumes per job
description ... without fabricating skills". A claim like that is only honest if something actually
verifies it. This file is that verification, and it is deliberately explicit about which parts of
the claim ARE covered and which are NOT (see test_documented_gaps_in_fabrication_prevention).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.resume import builder, quality, skills_source  # noqa: E402


# --------------------------------------------------------------- invented technology ----
def test_detects_tech_not_in_evidence():
    """A tech named in a bullet that is in neither the allow-list nor the evidence is flagged."""
    issues = quality.validate_bullet(
        "Built a streaming pipeline in Kafka to sync user state.",
        allowed_tech={"python", "fastapi"},
        evidence_text="Built a sync service in Python with FastAPI.")
    assert any("kafka" in i.lower() for i in issues), issues


def test_allows_tech_present_in_evidence():
    issues = quality.validate_bullet(
        "Built a FastAPI service backed by SQLite.",
        allowed_tech={"fastapi", "sqlite"},
        evidence_text="Built a FastAPI backend on SQLite.")
    assert not any("not in evidence" in i for i in issues), issues


# ------------------------------------------------------------------- invented numbers ----
def test_detects_number_not_in_evidence():
    issues = quality.validate_bullet(
        "Cut p99 latency by 45% across the fleet.",
        allowed_tech={"python"},
        evidence_text="Reduced latency of the sync path in Python.")
    assert any("'45'" in i for i in issues), issues


def test_allows_number_present_in_evidence():
    issues = quality.validate_bullet(
        "Onboarded 100 test users onto the app.",
        allowed_tech={"react native"},
        evidence_text="Shipped the app, onboarding 100 test users.")
    assert not any("not in evidence" in i for i in issues), issues


# --------------------------------------------------------- self-referential capability ----
@pytest.mark.parametrize("bullet", [
    "Built a tool that tailors resumes per job description without fabricating skills.",
    "Engineered a resume generator that accurately maps evidence to job requirements.",
    "Created an agent that reliably prevents hallucinated skills in generated resumes.",
    # Phrasings that leaked past the first version of this check — a real generated bullet said
    # "guaranteeing skills are not fabricated", which the adverb list alone did not catch.
    "Built an agent that tailors a resume per job description using a local LLM, "
    "guaranteeing skills are not fabricated.",
    "Shipped a resume pipeline ensuring no hallucinated skills reach the PDF.",
    "Built a resume tool that is hallucination-free across job descriptions.",
    "Engineered a resume generator where skills are never fabricated.",
    "Built a resume agent that avoids fabricating metrics.",
    # Second leak: ADJECTIVE forms. The adverb list alone missed "accurate skill representation",
    # which is the same unproven claim as "accurately represents skills".
    "Built an agent that tailors resumes per job description with accurate skill representation.",
    "Resume generator with correct skill mapping and precise evidence matching.",
    "Built a reliable resume tailoring pipeline for each job description.",
])
def test_flags_unbacked_capability_claims_about_this_pipeline(bullet):
    claims = quality.self_capability_claims(bullet)
    assert claims, f"should flag an unverified self-claim: {bullet}"


@pytest.mark.parametrize("bullet", [
    "Built a FastAPI and SQLite backend that runs entirely on one machine.",
    "Engineered a local LLM pipeline that drafts per-job resume text with Ollama.",
    "Wired a Chrome extension that reads a job posting and fills the application form.",
])
def test_allows_factual_pipeline_bullets(bullet):
    """Describing WHAT the tool does is fine — only claims about how WELL it does it need proof."""
    assert quality.self_capability_claims(bullet) == []


def test_strip_removes_the_unbacked_claim_but_keeps_the_fact():
    out, removed = quality.strip_self_capability_claims(
        "Built a tool that tailors resumes per job description without fabricating skills.")
    assert removed
    assert "tailors resumes per job description" in out
    assert "without fabricating" not in out.lower()


def test_strip_keeps_real_facts_that_follow_a_claim():
    """Stripping must not swallow a following clause carrying a genuine fact."""
    out, _ = quality.strip_self_capability_claims(
        "Built a resume tool with accurate matching, onboarding 100 test users.")
    assert "onboarding 100 test users" in out
    assert quality.self_capability_claims(out) == []


def test_strip_never_leaves_a_sentence_fragment():
    out, _ = quality.strip_self_capability_claims(
        "Resume generator with correct skill mapping and precise evidence matching.")
    assert out == "Resume generator."


def test_quality_adjectives_outside_self_referential_bullets_are_untouched():
    """'accurate turn-by-turn guidance' in a routing bullet is a description of the product, not a
    claim about this pipeline's correctness — scope must protect it."""
    b = ("Hand-mapped school floor plans in QGIS into a Dijkstra/A* routing graph for accurate "
         "turn-by-turn guidance.")
    assert quality.self_capability_claims(b) == []
    assert quality.strip_self_capability_claims(b)[0] == b


# --------------------------------------------- the claim the audit was actually about ----
def test_jd_skills_cannot_reach_the_skills_section():
    """THE substantive part of "without fabricating skills" that IS now verifiable: a job
    description naming skills the candidate has not verified cannot add them to the resume."""
    jd_only = ["Angular", "Oracle", "Excel", "Kubernetes", "Terraform"]
    groups = skills_source.ordered_groups(jd_only)
    flat = {s.lower() for items in groups.values() for s in items}
    verified = {r["name"].lower() for rows in skills_source.verified().values() for r in rows}
    assert flat <= verified, "skills section contained something not in the verified file"
    for kw in jd_only:
        assert kw.lower() not in flat, f"JD keyword {kw} leaked into the skills section"


def test_skills_section_is_stable_across_different_jds():
    """The same verified file must yield the same SET of skills regardless of the job — only the
    order may change. This is the bug where versions of one resume disagreed."""
    a = skills_source.ordered_groups(["Python", "FastAPI"])
    b = skills_source.ordered_groups(["Angular", "Oracle", "SAS"])
    flat = lambda g: {s for items in g.values() for s in items}
    assert flat(a) == flat(b)


def test_java_is_always_listed_under_languages():
    """User requirement (2026-08-17): Java must appear in the Skills section for every job,
    regardless of what the job description mentions."""
    for jd in ([], ["Python"], ["Angular", "Oracle"], ["COBOL", "Assembly"]):
        groups = skills_source.ordered_groups(jd)
        langs = groups.get("programming_languages") or []
        assert "Java" in langs, f"Java missing from languages for JD {jd}: {langs}"


def test_java_evidence_resolves_against_coursework():
    """Java is backed by coursework rather than a project, so the evidence check must accept a
    course line — otherwise it would warn on every single generation."""
    content = {
        "skills": skills_source.ordered_groups([]),
        "work": [], "projects": [],
        "education": [{"school": "University of Texas at Austin",
                       "coursework": ["Data Structures", "Discrete Math"]}],
    }
    warned = [w["skill"] for w in skills_source.evidence_warnings(content)]
    assert "Java" not in warned, f"Java should not warn when its coursework is on the resume: {warned}"


def test_quarantined_skills_are_never_rendered():
    review = {r["name"].lower() for rows in skills_source.needs_review().values() for r in rows}
    if not review:
        pytest.skip("nothing quarantined")
    groups = skills_source.ordered_groups(sorted(review))   # ask for them explicitly
    flat = {s.lower() for items in groups.values() for s in items}
    assert not (flat & review), f"needs_review skills rendered: {flat & review}"


# ------------------------------------------------------------------- documented gaps ----
def test_documented_gaps_in_fabrication_prevention():
    """Honest record of what is NOT prevented, so nobody reads the tests above as broader than
    they are. These assertions PASS while describing weaknesses — update them when the gap closes.
    """
    # GAP 1: detection is advisory. validate_bullet reports issues; _polish surfaces them in
    # gen["validation"] and does NOT drop or regenerate the offending bullet.
    issues = quality.validate_bullet("Built it in Kafka.", {"python"}, "Built it in Python.")
    assert issues                      # detected...
    # ...but nothing in the pipeline removes such a bullet; see builder._polish step 3.

    # GAP 2: invented-tech detection only covers jd_signals.TECH_VOCAB. A tool outside that fixed
    # vocabulary is invisible.
    from backend.resume import jd_signals
    assert "snowflake" not in jd_signals.TECH_VOCAB
    assert quality.validate_bullet("Modeled data in Snowflake.", {"python"}, "Modeled data.") == []

    # GAP 3: single-digit numbers are exempt, so a small fabricated count is not reported as
    # invented (isolate from the length check by using a full-length bullet).
    gap3 = quality.validate_bullet(
        "Led 4 engineers through a Python rewrite of the ingestion service.",
        {"python"}, "Rewrote the ingestion service in Python.")
    assert not any("not in evidence" in i for i in gap3), gap3
