"""Local embeddings via Ollama nomic-embed-text. Powers Q&A semantic recall — fast path,
never the LLM. Pure-Python cosine keeps the dependency surface tiny.

Perf note (2026-08-25): on a 24GB Mac, gemma3:12b alone holds ~9–10GB. Leaving the embedder
resident at the same time forces memory pressure / swap, and every gemma↔nomic switch reloads
weights (~10–20s). keep_alive for embeds is short; call unload() before any chat completion so
the chat model gets the RAM.
"""
import math
import httpx
from . import config

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def embed(text: str, task: str = "classification") -> list[float]:
    """Embed text. nomic-embed-text REQUIRES a task prefix or short strings collapse into a
    near-identical similarity band. We use 'classification' on BOTH sides for symmetric
    question<->question matching — empirically the cleanest separation between paraphrases
    (>=0.85) and unrelated questions (<=0.74) for free-text application questions."""
    prompt = f"{task}: {text}"
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(
            f"{config.OLLAMA_URL}/api/embeddings",
            json={
                "model": config.EMBED_MODEL,
                "prompt": prompt,
                # Short residency: warm across the ~20 embeds in one phase, then llm.chat unloads.
                "keep_alive": getattr(config, "EMBED_KEEP_ALIVE", "60s"),
            },
        )
        r.raise_for_status()
        return r.json()["embedding"]


def unload() -> None:
    """Drop the embedder from memory so the chat model is not fighting it for RAM/swap.

    No-ops when nomic isn't loaded — the unload round-trip itself was costing ~0.6s per chat
    call even when there was nothing to unload.
    """
    try:
        with httpx.Client(timeout=5.0) as c:
            ps = c.get(f"{config.OLLAMA_URL}/api/ps").json()
            names = {m.get("name", "") for m in (ps.get("models") or [])}
            if not any(n.startswith(config.EMBED_MODEL) for n in names):
                return
            c.post(
                f"{config.OLLAMA_URL}/api/embeddings",
                json={"model": config.EMBED_MODEL, "prompt": ".", "keep_alive": 0},
            )
    except Exception:
        pass


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
