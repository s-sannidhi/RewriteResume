"""Lightweight stage timings for the JD → resume → cover pipeline.

Printed as one block to server.log so a slow job is diagnosable without grepping
scattered chat lines. Stages accumulate on a thread-local so nested helpers can
contribute without threading a timer through every call.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

_tls = threading.local()


def _bucket() -> dict:
    b = getattr(_tls, "bucket", None)
    if b is None:
        b = {"stages": {}, "meta": {}, "t0": time.time()}
        _tls.bucket = b
    return b


def reset(label: str = "") -> None:
    _tls.bucket = {"stages": {}, "meta": {"label": label}, "t0": time.time()}


def add(stage: str, seconds: float, **meta) -> None:
    b = _bucket()
    b["stages"][stage] = b["stages"].get(stage, 0.0) + float(seconds)
    if meta:
        b["meta"].update(meta)


@contextmanager
def stage(name: str):
    t0 = time.time()
    try:
        yield
    finally:
        add(name, time.time() - t0)


def note(**meta) -> None:
    _bucket()["meta"].update(meta)


def summary(total: float | None = None) -> str:
    b = _bucket()
    stages = b["stages"]
    tot = total if total is not None else (time.time() - b["t0"])
    accounted = sum(stages.values())
    other = max(0.0, tot - accounted)
    label = b["meta"].get("label") or "generation"
    lines = [f"pipeline [{label}] total={tot:.1f}s"]
    # Stable order for the three LLM legs + prep work.
    order = [
        "analyze_llm", "analyze_other",
        "evidence_embed", "resume_llm", "resume_assemble", "resume_pdf",
        "cover_llm", "cover_pdf",
        "json_retry",
    ]
    seen = set()
    for k in order:
        if k in stages:
            lines.append(f"  {k}: {stages[k]:.1f}s")
            seen.add(k)
    for k, v in sorted(stages.items()):
        if k not in seen:
            lines.append(f"  {k}: {v:.1f}s")
    if other >= 0.05:
        lines.append(f"  other: {other:.1f}s")
    # Extra diagnostics when present.
    for key in ("tok_s", "prompt_tok", "gen_tok", "json_retries", "free_mb"):
        if key in b["meta"]:
            lines.append(f"  {key}={b['meta'][key]}")
    return "\n".join(lines)


def log_summary(total: float | None = None) -> str:
    msg = summary(total)
    print(msg, flush=True)
    return msg
