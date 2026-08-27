"""Q&A semantic memory. When a form asks a question, we recall the closest past answer by
embedding cosine similarity — instant, no LLM. The user confirms/edits, and we save it for
next time. This is resolution step 2 in the autofill chain (after direct profile map).
"""
import json
import re
import time
import uuid
from . import db
from .. import config, embeddings


def _norm(s: str) -> str:
    """Normalize a label/question for lexical comparison (drops '*', punctuation, case)."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def recall(question: str, field_type: str | None = None, threshold: float | None = None) -> dict | None:
    """Best past answer for a semantically similar question, or None if nothing clears the bar."""
    if not question.strip():
        return None
    thr = config.QA_MATCH_THRESHOLD if threshold is None else threshold
    rows = db.conn().execute(
        "SELECT id,question,answer,field_type,embedding,usage_count FROM qa_memory"
    ).fetchall()

    # Lexical short-circuit: identical labels (ignoring '*'/punctuation) are an exact recall.
    # This is the common repeat-form case and dodges short-label embedding collapse.
    nq = _norm(question)
    if nq:
        for r in rows:
            if _norm(r["question"]) == nq and (r["answer"] or "").strip():
                return {"id": r["id"], "question": r["question"], "answer": r["answer"],
                        "field_type": r["field_type"], "similarity": 1.0,
                        "usage_count": r["usage_count"]}

    qv = embeddings.embed(question)  # symmetric 'classification' on both sides
    best, best_sim = None, 0.0
    for r in rows:
        if not (r["answer"] or "").strip():
            continue  # an empty stored answer is never a useful recall
        emb = json.loads(r["embedding"] or "[]")
        sim = embeddings.cosine(qv, emb)
        # small bonus when the field type matches (e.g. both "textarea"/"select")
        if field_type and r["field_type"] == field_type:
            sim += 0.02
        if sim > best_sim:
            best, best_sim = r, sim
    if best is None or best_sim < thr:
        return None
    # Lexical guard: short labels embed into a tight band, so a high cosine with ZERO shared
    # words is almost always a false match (e.g. "Desired Salary" ~ "Postal Code"). Require
    # at least one shared content word when the query is short. Essay-length queries pass.
    q_tokens = set(nq.split())
    if len(q_tokens) <= 4 and not (q_tokens & set(_norm(best["question"]).split())):
        return None
    return {
        "id": best["id"],
        "question": best["question"],
        "answer": best["answer"],
        "field_type": best["field_type"],
        "similarity": round(best_sim, 4),
        "usage_count": best["usage_count"],
    }


def save(question: str, answer: str, field_type: str | None = None) -> dict:
    """Store a confirmed answer (with its embedding). Updates the answer if the exact
    question already exists rather than duplicating it."""
    c = db.conn()
    existing = c.execute(
        "SELECT id FROM qa_memory WHERE question = ?", (question,)
    ).fetchone()
    emb = json.dumps(embeddings.embed(question))
    if existing:
        c.execute(
            "UPDATE qa_memory SET answer=?, field_type=?, embedding=? WHERE id=?",
            (answer, field_type, emb, existing["id"]),
        )
        c.commit()
        return {"id": existing["id"], "updated": True}
    qid = uuid.uuid4().hex[:12]
    c.execute(
        "INSERT INTO qa_memory(id,question,answer,field_type,embedding,created_at,usage_count)"
        " VALUES(?,?,?,?,?,?,0)",
        (qid, question, answer, field_type, emb, _now()),
    )
    c.commit()
    return {"id": qid, "updated": False}


def bump_usage(qa_id: str) -> None:
    c = db.conn()
    c.execute("UPDATE qa_memory SET usage_count = usage_count + 1 WHERE id = ?", (qa_id,))
    c.commit()


def count() -> int:
    return db.conn().execute("SELECT COUNT(*) n FROM qa_memory").fetchone()["n"]


def reembed_all() -> int:
    """Recompute every embedding from its question text with the current model. One-time
    repair for rows migrated with incompatible v1 vectors. Returns rows updated."""
    c = db.conn()
    rows = c.execute("SELECT id,question FROM qa_memory").fetchall()
    n = 0
    for r in rows:
        if not (r["question"] or "").strip():
            continue
        c.execute("UPDATE qa_memory SET embedding=? WHERE id=?",
                  (json.dumps(embeddings.embed(r["question"])), r["id"]))
        n += 1
    c.commit()
    return n


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
