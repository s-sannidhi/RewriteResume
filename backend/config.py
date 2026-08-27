"""Central config. Single-user, local-only. Nothing here is a secret beyond the host machine."""
from pathlib import Path

# --- Runtime data lives outside the repo so a rebuild never touches it ---
DATA_DIR = Path.home() / "ResumeRewriter"
PROFILE_PATH = DATA_DIR / "profile.json"
RESUMES_DIR = DATA_DIR / "resumes"
DOCUMENTS_DIR = DATA_DIR / "documents"  # frequently-uploaded static docs (transcript, schedule, …)
DB_PATH = DATA_DIR / "rr.db"            # v2 SQLite store (Q&A memory + tracker)

# Legacy v1 JSON stores — migrated into SQLite on first run, then left untouched.
LEGACY_QA_PATH = DATA_DIR / "qa_memory.json"
LEGACY_TRACKER_PATH = DATA_DIR / "tracker.json"
LEGACY_SITE_MEMORY_PATH = DATA_DIR / "site_memory.json"

# --- Models (local Ollama) ---
OLLAMA_URL = "http://127.0.0.1:11434"
LLM_MODEL = "gemma3:12b"          # switched from qwen2.5:14b 2026-08-05 (user's call)
# Ollama defaults num_ctx to 4096, and it TRUNCATES rather than erroring when prompt+reply pass
# it. The resume prompt is ~3.8k tokens and the JSON reply is typically <2k, so 8192 fits with
# headroom. 16384 was previously used "just in case" but every ctx bump forces a model reload
# (~5–15s) and a colder prompt-eval path — a real contributor to multi-minute generations.
# 6144 fits the ~3.8k resume prompt + ~2k reply with less KV-cache RAM than 8192 — matters on
# a 24GB Mac where gemma3:12b already holds ~9–10GB and swap kills tok/s.
# Fit measured resume calls (≈2.5k prompt + ≈0.6k reply ≈ 3.1k). 5120 leaves headroom for
# feedback passes without the KV-cache RAM of 6144/8192 — on a 24GB Mac under Chrome-batch
# load, the extra KV was pushing the machine into swap and cutting tok/s roughly in half.
# FIXED across analyze/resume/cover — never change per-call (reload wipe).
LLM_CTX = 5120
# Keep the chat model resident between analyze → resume → cover-letter so we don't pay the
# load cost three times per job.
LLM_KEEP_ALIVE = "30m"
# Hard caps on output length. Without these, a chatty local model can wander for minutes at
# ~6 tok/s. Sized for each call site's expected JSON/prose. Resume replies measure ~500–650
# tokens; 1400 is enough headroom without inviting a long ramble under slow tok/s.
LLM_PREDICT_ANALYZE = 700
LLM_PREDICT_RESUME = 1400
LLM_PREDICT_COVER = 450
LLM_PREDICT_DEFAULT = 900
# Analyze only needs the signal-bearing part of a JD. 8k chars (~2k tok) was overkill and
# slowed prompt-eval under memory pressure; 5k still covers every real posting we see.
JD_ANALYZE_CHARS = 5000
EMBED_MODEL = "nomic-embed-text"
# Keep nomic warm across the short burst of embeds in one phase (cold-cache profile = many calls).
# llm.chat() / builder explicitly unload() before every completion so gemma isn't co-resident.
# Do NOT set this to "0" — that reloads nomic on every single embed call.
EMBED_KEEP_ALIVE = "30s"

# Cache of profile-bullet embeddings so evidence ranking costs ~0 extra time per generation
# (only new/edited bullets get embedded; the JD is embedded once per run). See resume/evidence.py.
BULLET_EMB_CACHE = DATA_DIR / "bullet_emb_cache.json"

# --- Server ---
HOST = "127.0.0.1"
PORT = 8765

# --- Q&A semantic recall ---
QA_MATCH_THRESHOLD = 0.80   # cosine above which we surface a past answer (classification embeds;
                            # paraphrases land >=0.85, unrelated <=0.74 — 0.80 sits in the gap)

# Resume reuse cache + generic-resume fallback were removed 2026-08-17 (user's call): every job now
# gets a freshly generated, tailored resume. Don't reintroduce a "match a past resume" shortcut —
# serving a resume built for a different posting was the failure mode that got them cut.

RESUMES_DIR.mkdir(parents=True, exist_ok=True)
DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
