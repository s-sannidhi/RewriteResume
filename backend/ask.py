"""Ask tab backend — application-question answers via the local Ollama model (same one used for
résumé rewriting and everything else in the system; no remote calls anywhere).
"""
import re

from . import config, llm, profile_store
from .store import qa_memory

_OLLAMA_HINT = (f"Can't reach the local model. Make sure Ollama is running (`ollama serve`) and "
                f"`{config.LLM_MODEL}` is pulled (`ollama pull {config.LLM_MODEL}`).")

# Distilled from the blader/humanizer skill (github.com/blader/humanizer) — strips AI-writing
# tells so answers read like a real person. Keeps its no-fabrication rule.
_HUMANIZE = (
    "WRITE LIKE A REAL PERSON, NOT AI. Non-negotiable:\n"
    "- NO em or en dashes (—, –) anywhere. Use a period, comma, colon, or parentheses. "
    "This is the #1 AI tell — zero tolerance; scan the final text and rewrite out any dash.\n"
    "- Banned words/phrases: delve, crucial, pivotal, key (as filler), tapestry, testament, "
    "underscore(s), showcase, foster(ing), garner, landscape, vibrant, intricate, interplay, "
    "realm, seamless, robust, leverage, elevate, myriad, navigate the, embark, boasts, "
    "'stands/serves as a testament', 'plays a vital/crucial role', 'it's not just X, it's Y', "
    "'not only... but also', 'when it comes to', 'at the end of the day', 'that being said', "
    "'a wide range of', 'in today's ... world', 'it is important to note'.\n"
    "- No signposting ('let's dive in', 'here's the thing', 'the real question is'), no "
    "conversational hooks ('honestly', 'look', 'real talk'), no filler ('in order to', 'due to "
    "the fact that').\n"
    "- Don't force groups of three, don't use negative parallelism, don't cycle synonyms for the "
    "same thing, don't manufacture drama with stacked short fragments.\n"
    "- Vary sentence length. Plain and specific beats polished. A little imperfection is fine.\n"
    "- Never add a fact, name, number, date, or claim that isn't true of the person."
)


def _strip_placeholders(text: str) -> str:
    """Remove bracketed fill-in artifacts the local model sometimes leaves (e.g. '[Company
    Name]', '[mention something specific]') and hard-enforce the humanizer's no-dash rule."""
    text = re.sub(r"\s*[\[\(][^\]\)]*(?:mention|insert|company name|your \w|e\.g\.|specific "
                  r"about)[^\]\)]*[\]\)]", "", text or "", flags=re.I)
    text = re.sub(r"\s*[—–]\s*", ", ", text)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"\s{2,}", " ", text)
    return re.sub(r"\s+([,.!?])", r"\1", text).strip()


def _generate(system: str, user: str) -> str:
    """Chat completion with one retry if the model leaves a placeholder bracket — regenerating
    reads far better than the regex strip, which can leave grammar orphaned around the cut
    (e.g. 'interested in's work in'). The strip still runs after, as a last-resort safety net."""
    out = llm.chat(system, user, temperature=0.3)
    if "[" in out or "]" in out:
        retry = llm.chat(
            system + "\n\nYour last attempt left a placeholder bracket in the text. Do not do "
                      "that — write the final answer with no brackets at all.",
            user, temperature=0.3)
        if "[" not in retry and "]" not in retry:
            out = retry
    return _strip_placeholders(out)


def _bullets(profile: dict) -> str:
    lines = []
    for w in profile.get("work_experience", []):
        head = f'{w.get("title","")} at {w.get("company","")} ({w.get("start_date","")}–{w.get("end_date") or "present"})'
        lines.append(head + "".join(f"\n  - {b}" for b in w.get("bullets", []) if b.strip()))
    for p in profile.get("projects", []):
        head = f'Project {p.get("name","")} [{", ".join(p.get("tech_stack", []))}]'
        lines.append(head + "".join(f"\n  - {b}" for b in p.get("bullets", []) if b.strip()))
    return "\n".join(lines)


def _system(profile: dict, jd_analysis: dict | None) -> str:
    ident = profile.get("identity", {})
    auth = profile.get("work_auth", {})
    ra = profile.get("reusable_answers", {})
    skills = sorted(profile_store.all_skills(profile) - profile_store.blocklist(profile))
    parts = [
        "You answer job-application questions AS the candidate, in first person. Truthful — "
        "use only the facts below, never invent experience, numbers, or skills. Concise and "
        "specific. Default to 3-6 sentences unless the question clearly needs more or less. "
        "Output ONLY the answer text, ready to paste — no preamble, no markdown headers, no "
        "meta-commentary. Never leave a placeholder, bracket, or fill-in instruction like "
        "'[Company Name]' or '[mention something specific]' — if you don't know a detail "
        "(the company name, a specific product), write a complete general sentence that doesn't "
        "need it. Never end a sentence early or trail off mid-thought ('particularly regarding.') "
        "— every sentence you write must be grammatically complete.",
        _HUMANIZE,
        f"\nCandidate: {ident.get('legal_name','')} ({ident.get('location','')}), CS student.",
        f"Work authorization: {auth.get('us_work_auth_status','')}, "
        f"needs sponsorship: {auth.get('needs_sponsorship','')}.",
        f"\nExperience and projects (evidence):\n{_bullets(profile)}",
        f"\nSkills: {', '.join(skills)}",
    ]
    if ra:
        canned = "\n".join(f"- {k}: {v}" for k, v in ra.items() if (v or "").strip())
        if canned:
            parts.append(f"\nThe candidate's own reusable answers (reuse their substance/voice):\n{canned}")
    if jd_analysis:
        parts.append(
            f"\nThe job being applied to — company: {jd_analysis.get('company','')}, "
            f"role: {jd_analysis.get('role_title','')}, summary: {jd_analysis.get('summary','')}, "
            f"tech: {', '.join(jd_analysis.get('concrete_tech', [])[:12])}"
        )
    return "\n".join(parts)


def _prompt(question: str, history: list[dict] | None, refine: str | None) -> str:
    """The local model is one-shot, so Refine folds the previous turn into the prompt."""
    if not (refine and history):
        return f"Application question:\n{question}"
    prior = "\n\n".join(
        f'{"Question" if m.get("role") == "user" else "Your previous answer"}:\n{m.get("content","")}'
        for m in history if m.get("role") in ("user", "assistant"))
    return (f"{prior}\n\nRevise your previous answer. Instruction: {refine}\n"
            "Output only the revised answer.")


def _chat_system(profile: dict) -> str:
    """General-purpose grounding for the website AI Ask chat — not job-specific, so it works for
    club apps, scholarships, essays, messages, brainstorming, anything."""
    ident = profile.get("identity", {})
    parts = [
        f"You are a writing and application assistant for {ident.get('legal_name','the user')}, "
        "a CS student. Help with whatever they ask: application questions (jobs, clubs, "
        "scholarships, programs), essays, cover notes, messages, brainstorming, editing. When "
        "writing AS them, use first person and ONLY real facts from their background below — "
        "never invent experience, numbers, or credentials. When they just want help or feedback, "
        "be direct and useful. Output only the response, ready to use. Never leave a placeholder, "
        "bracket, or fill-in instruction like '[Company Name]' — write around an unknown detail "
        "in general terms instead.",
        _HUMANIZE,
        f"\nTheir background (facts — use only what's true):\n{_bullets(profile)}",
        f"\nSkills: {', '.join(sorted(profile_store.all_skills(profile)))}",
    ]
    ra = profile.get("reusable_answers", {})
    canned = "\n".join(f"- {k}: {v}" for k, v in ra.items() if (v or "").strip())
    if canned:
        parts.append(f"\nTheir own reusable answers (reuse their substance/voice):\n{canned}")
    return "\n".join(parts)


def chat_reply(messages: list[dict]) -> dict:
    """Multi-turn chat for the website AI Ask mode. `messages` is the full thread
    [{role:'user'|'assistant', content}]. Grounded in the profile, humanized. The local model is
    one-shot per call, so the whole conversation is folded into the prompt."""
    convo = "\n\n".join(
        f'{"User" if m.get("role") == "user" else "Assistant"}: {m.get("content", "")}'
        for m in messages if m.get("role") in ("user", "assistant"))
    try:
        out = _generate(_chat_system(profile_store.load()), convo + "\n\nAssistant:")
    except Exception as e:
        return {"error": "llm_failed", "hint": f"{_OLLAMA_HINT} ({e})"}
    if not out:
        return {"error": "llm_failed", "hint": _OLLAMA_HINT}
    return {"answer": out}


def answer(question: str, jd_analysis: dict | None = None,
           history: list[dict] | None = None, refine: str | None = None) -> dict:
    recall = None
    try:
        recall = qa_memory.recall(question)
    except Exception:
        pass  # embeddings (Ollama) down — Ask should still work

    try:
        out = _generate(_system(profile_store.load(), jd_analysis),
                        _prompt(question, history, refine))
    except Exception as e:
        return {"error": "llm_failed", "hint": f"{_OLLAMA_HINT} ({e})", "recall": recall}
    if not out:
        return {"error": "llm_failed", "hint": _OLLAMA_HINT, "recall": recall}
    return {"answer": out, "model": config.LLM_MODEL, "recall": recall}
