"""The project's ONLY model: a local one served by Ollama (config.LLM_MODEL, gemma3:12b).
Nothing here calls a hosted API — no Anthropic, no OpenAI — so every generated word is
produced on this machine. The only slow path in the system, so it is used sparingly: resume
rewriting, JD analysis, Ask, chat, cover letters. Everything else is lookup.
"""
import json
import logging
import re
import time
import httpx
from . import config, embeddings, timing

_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
_log = logging.getLogger("rr.llm")
# LaunchAgent writes stdout/stderr to ~/ResumeRewriter/server.log — make timings visible there.
if not _log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


def _free_mb() -> int | None:
    """Rough free+purgeable RAM in MB (macOS). None if unavailable. Under ~200MB, gemma tok/s
    typically halves because the OS is compressing/swapping around the 8GB model weights."""
    try:
        import subprocess
        out = subprocess.check_output(["vm_stat"], text=True, timeout=1)
        page = 16384
        free = purge = 0
        for line in out.splitlines():
            if "Pages free" in line:
                free = int(line.split(":")[1].strip().rstrip("."))
            elif "Pages purgeable" in line:
                purge = int(line.split(":")[1].strip().rstrip("."))
        return int((free + purge) * page / 1e6)
    except Exception:
        return None


def chat(system: str, user: str, *, temperature: float = 0.4, json_mode: bool = False,
         num_predict: int | None = None) -> str:
    """Single-shot chat completion. Returns raw assistant text."""
    # Free the embedder first so gemma isn't co-resident with nomic under RAM pressure.
    embeddings.unload()
    pred = int(num_predict if num_predict is not None else config.LLM_PREDICT_DEFAULT)
    # FIXED num_ctx for every call. Changing it between analyze→resume→cover forces Ollama to
    # reload gemma (~5s) and wipe the warm KV cache — measured prompt-eval jumps from ~0.1s
    # (same ctx) to ~10s+ (ctx change). A slightly oversized ctx on short calls is cheaper.
    ctx = int(config.LLM_CTX)
    options = {"temperature": temperature, "num_ctx": ctx, "num_predict": pred}
    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "keep_alive": config.LLM_KEEP_ALIVE,
        "options": options,
    }
    if json_mode:
        payload["format"] = "json"
    free_mb = _free_mb()
    t0 = time.time()
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(f"{config.OLLAMA_URL}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()
    dt = time.time() - t0
    eval_n = data.get("eval_count") or 0
    prompt_n = data.get("prompt_eval_count") or 0
    load_s = (data.get("load_duration") or 0) / 1e9
    prompt_s = (data.get("prompt_eval_duration") or 0) / 1e9
    gen_s = (data.get("eval_duration") or 0) / 1e9
    gen_tps = (eval_n / gen_s) if gen_s > 0 else 0
    free_bit = f" free≈{free_mb}MB" if free_mb is not None else ""
    warn = ""
    if free_mb is not None and free_mb < 200:
        warn = " ⚠LOW_RAM"
    if gen_tps and gen_tps < 18:
        warn += " ⚠SLOW_GEN"
    msg = (f"chat {dt:.1f}s (load={load_s:.1f}s prompt={prompt_s:.1f}s/{prompt_n}tok "
           f"gen={gen_s:.1f}s/{eval_n}tok {gen_tps:.1f}t/s ctx={ctx} predict={pred} "
           f"json={json_mode}{free_bit}{warn})")
    _log.info(msg)
    print(msg, flush=True)  # also to LaunchAgent server.log
    return data["message"]["content"]


def chat_json(system: str, user: str, *, temperature: float = 0.3,
              num_predict: int | None = None) -> dict:
    """Chat constrained to JSON. Tolerates models that wrap JSON in prose/code fences.

    Under RAM pressure the model often returns *almost*-valid JSON (trailing comma, cut-off
    brace). A full LLM retry doubles analyze time (~20–25s). Try cheap local repairs first.
    """
    pred = num_predict if num_predict is not None else config.LLM_PREDICT_DEFAULT
    raw = chat(system, user, temperature=temperature, json_mode=True, num_predict=pred)
    parsed = _loads(raw)
    if parsed is not None:
        return parsed
    repaired = _repair_json(raw)
    if repaired is not None:
        print("chat_json: repaired locally (skipped LLM retry)", flush=True)
        timing.add("json_repair", 0.0, json_retries=0)
        return repaired
    # One retry. A local model occasionally closes a brace wrong or stops a token early, and a
    # second pass costs far less than failing the whole generation back to the user — but under
    # memory pressure it is expensive, so we only do it when local repair failed.
    print("chat_json: invalid JSON — retrying once", flush=True)
    t0 = time.time()
    retry = chat(system + "\n\nYour last reply was not valid JSON. Reply with ONE complete, valid "
                          "JSON object and nothing else.", user, temperature=temperature,
                 json_mode=True, num_predict=pred)
    timing.add("json_retry", time.time() - t0, json_retries=1)
    parsed = _loads(retry) or _repair_json(retry)
    if parsed is not None:
        return parsed
    raise ValueError(
        f"the model did not return valid JSON after a retry (last reply ended: ...{retry[-80:]!r}). "
        f"If it looks cut off, raise config.LLM_CTX.")


def _loads(raw: str):
    """Parse, tolerating a model that wraps JSON in prose or code fences. None if unparseable."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}|\[.*\]", raw or "", re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _repair_json(raw: str):
    """Best-effort fix for the common local-model failures: trailing commas, truncated close."""
    if not raw or not raw.strip():
        return None
    m = re.search(r"[\{\[].*", raw, re.DOTALL)
    if not m:
        return None
    s = m.group(0)
    # Drop trailing commas before } or ]
    s = re.sub(r",\s*([}\]])", r"\1", s)
    # Balance braces/brackets if the model stopped mid-object.
    opens = s.count("{") - s.count("}")
    opens_b = s.count("[") - s.count("]")
    if opens > 0 or opens_b > 0:
        # Trim a partial trailing string/key so the closers land cleanly.
        s = re.sub(r",\s*\"[^\"]*$", "", s)
        s = re.sub(r":\s*\"[^\"]*$", ': ""', s)
        s = re.sub(r",\s*$", "", s)
        s = s + ("]" * max(0, opens_b)) + ("}" * max(0, opens))
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def healthy() -> bool:
    try:
        with httpx.Client(timeout=5.0) as c:
            tags = c.get(f"{config.OLLAMA_URL}/api/tags").json()
        names = {m["name"] for m in tags.get("models", [])}
        return any(n.startswith(config.LLM_MODEL) for n in names)
    except Exception:
        return False
