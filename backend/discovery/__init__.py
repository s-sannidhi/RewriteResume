"""Startup discovery (outreach Phase 1).

Find promising startups worth contacting even when they have no public posting. The pipeline is
sources -> normalize -> rank -> (lazy) contact enrichment, mirroring the intern-feed pattern
(scrape -> _fit -> tracker). Scoring is deterministic Python — no LLM — consistent with the
resume-quality philosophy (Python owns facts/selection/scoring).

Anchor source: Y Combinator's public company index. Sources are pluggable (see sources.py).
"""
