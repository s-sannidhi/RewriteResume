"""Smart evidence selection (#1, #7) — send the LLM only the strongest, most JD-relevant bullets
instead of everything, using SEMANTIC similarity (nomic-embed) not keyword overlap.

Time budget: profile bullets are embedded ONCE and cached (in-memory + disk by text hash), so a
normal generation only embeds the JD query (one call). Editing a bullet re-embeds just that one
next run. A leaner prompt also tends to make the LLM call itself a touch faster — so this is
time-neutral or better, never slower.

Critical perf note (2026-08-25): the on-disk cache had grown to ~21MB / 1300+ entries because
EVERY rewritten bullet was persisted. Each embed_bullets() call re-read that file (~0.9s), and
assemble/polish alone did it ~12 times (~10s of pure JSON I/O per generation). Fix: keep the
cache in process memory, only persist profile/evidence texts (persist=True), and refuse to reload
a bloated cache from disk.
"""
import hashlib
import json
import threading

from .. import config, embeddings
from . import quality

# Process-lifetime cache. Disk is a warm-start aid, not the source of truth on every call.
_mem: dict | None = None
_persist_keys: set[str] = set()
_mem_lock = threading.Lock()
_dirty = False

# Past this, the file is almost certainly full of ephemeral rewritten-bullet vectors. Dropping it
# costs one re-embed of the profile (~1–2s once); keeping it costs ~1s per load forever.
_MAX_DISK_BYTES = 4_000_000
_MAX_DISK_ENTRIES = 400


def _key(text: str) -> str:
    return hashlib.sha1((text or "").strip().lower().encode("utf-8")).hexdigest()


def _load_cache() -> dict:
    global _mem
    if _mem is not None:
        return _mem
    with _mem_lock:
        if _mem is not None:
            return _mem
        try:
            path = config.BULLET_EMB_CACHE
            if path.exists() and path.stat().st_size > _MAX_DISK_BYTES:
                # Bloated by ephemeral rewritten bullets — start clean.
                _mem = {}
                _persist_keys.clear()
            else:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and len(raw) <= _MAX_DISK_ENTRIES:
                    _mem = raw
                    _persist_keys.update(raw.keys())
                else:
                    _mem = {}
                    _persist_keys.clear()
        except Exception:
            _mem = {}
            _persist_keys.clear()
        return _mem


def _save_cache() -> None:
    """Write ONLY durable (persist=True) vectors — never ephemeral rewrite embeddings."""
    global _dirty
    cache = _mem or {}
    durable = {k: cache[k] for k in _persist_keys if k in cache}
    if len(durable) > _MAX_DISK_ENTRIES:
        durable = dict(list(durable.items())[:_MAX_DISK_ENTRIES])
    try:
        config.BULLET_EMB_CACHE.write_text(json.dumps(durable), encoding="utf-8")
        _dirty = False
    except Exception:
        pass


def embed_bullets(bullets: list[str], *, persist: bool = True) -> dict[str, list[float]]:
    """text -> embedding, from the in-memory cache; misses are embedded once.

    persist=True (default): write new vectors to disk — use for stable profile evidence.
    persist=False: memory only — use for rewritten/ephemeral bullets so the disk cache stays small.
    Never raises.
    """
    global _dirty
    cache = _load_cache()
    out, dirty = {}, False
    for b in bullets:
        if not b or not b.strip():
            continue
        k = _key(b)
        vec = cache.get(k)
        if vec is None:
            try:
                vec = embeddings.embed(b)
            except Exception:
                vec = []
            cache[k] = vec
            dirty = True
        out[b] = vec
        if persist:
            _persist_keys.add(k)
    if dirty and persist:
        _dirty = True
        _save_cache()
    return out


def jd_embedding(query: str) -> list[float]:
    if not (query or "").strip():
        return []
    try:
        return embeddings.embed(query)
    except Exception:
        return []


def _norm(vals: list[float]) -> list[float]:
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return [0.5] * len(vals)
    return [(v - lo) / (hi - lo) for v in vals]


def semantic_redundant_pairs(bullets: list[str], threshold: float = 0.86) -> list[dict]:
    """Bullet pairs in ONE entry that mean the same thing, by embedding cosine.

    This is the half of the cross-bullet duplication check that token overlap cannot do: two
    rewrites of one source fact can share few literal words ("cross-platform mobile app with React
    Native" vs "React Native app built on Firebase") yet be the same accomplishment. Embeddings for
    rewritten bullets are memory-only (persist=False) so they don't bloat the disk cache.
    Returns [] if embeddings are unavailable — never blocks generation.
    """
    real = [b for b in bullets if (b or "").strip()]
    if len(real) < 2:
        return []
    try:
        embs = embed_bullets(real, persist=False)
    except Exception:
        return []
    out = []
    for i in range(len(real)):
        for j in range(i + 1, len(real)):
            ea, eb = embs.get(real[i]) or [], embs.get(real[j]) or []
            if not ea or not eb:
                continue
            sim = embeddings.cosine(ea, eb)
            if sim >= threshold:
                out.append({"keep": i, "redundant": j, "similarity": round(sim, 4)})
    return out


def nearest_source(bullet: str, sources: list[str]) -> tuple[int, float]:
    """(index, similarity) of the evidence bullet this rewritten bullet came from. Used to tell
    'two bullets from the same source fact' apart from 'two bullets about related work'."""
    ranked = nearest_sources([bullet], sources)
    return ranked[0] if ranked else (-1, 0.0)


def nearest_sources(bullets: list[str], sources: list[str]) -> list[tuple[int, float]]:
    """Batch form of nearest_source — one embed pass for the whole entry, not one per bullet."""
    if not sources or not bullets:
        return [(-1, 0.0)] * len(bullets)
    try:
        embs = embed_bullets(list(bullets) + list(sources), persist=False)
    except Exception:
        return [(-1, 0.0)] * len(bullets)
    out = []
    for bullet in bullets:
        be = embs.get(bullet) or []
        if not be:
            out.append((-1, 0.0))
            continue
        best, best_sim = -1, -1.0
        for i, s in enumerate(sources):
            se = embs.get(s) or []
            if not se:
                continue
            sim = embeddings.cosine(be, se)
            if sim > best_sim:
                best, best_sim = i, sim
        out.append((best, best_sim))
    return out


def select(evidence: list[str], jd_emb: list[float], bull_embs: dict[str, list[float]],
           keep: int, protected: list[str] | None = None) -> list[str]:
    """Return the top `keep` evidence bullets by 0.6*JD-relevance + 0.4*impact (both min-max
    normalized within this entry). Protected bullets (those carrying a must-keep parenthetical) are
    ALWAYS included regardless of score. Preserves the original relative order of the winners so the
    prompt still reads naturally. Falls back to impact-only if embeddings are unavailable."""
    ev = [b for b in evidence if b and b.strip()]
    protected = set(protected or [])
    if len(ev) <= keep:
        return ev

    rel = []
    for b in ev:
        e = bull_embs.get(b) or []
        rel.append(embeddings.cosine(jd_emb, e) if (jd_emb and e) else 0.0)
    imp = [quality.impact_score(b) for b in ev]
    rn, inorm = _norm(rel), _norm(imp)
    scored = [(0.6 * rn[i] + 0.4 * inorm[i], i) for i in range(len(ev))]

    chosen = {i for i, b in enumerate(ev) if b in protected}          # protected first
    for _, i in sorted(scored, reverse=True):
        if len(chosen) >= keep:
            break
        chosen.add(i)
    # keep original order among the winners
    return [ev[i] for i in range(len(ev)) if i in chosen]
