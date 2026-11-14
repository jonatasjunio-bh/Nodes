"""
Deep think — hands a hard request to the second brain (core/deep_brain.py).

The voice model answers at once ("deixa comigo"), the conversation carries on,
and the finished answer is brought back when the user isn't talking: spoken in
a few sentences, with the details on the HUD's content panel.

    "pesquisa e compara os três melhores notebooks até 5 mil"
    "monta um roteiro de 3 dias em Lisboa"
    "quanto vou gastar de gasolina indo a BH, com o preço de hoje?"
"""

from __future__ import annotations

import threading
import time

_running: dict[int, str] = {}       # job id → task, for "what are you working on?"
_next_id = 0
_lock = threading.Lock()


def _work(job: int, task: str, context: str, player) -> None:
    from core import deep_brain
    t0 = time.monotonic()
    try:
        spoken, detail = deep_brain.think(task, context)
    except Exception as e:
        spoken, detail = f"A análise falhou: {e}", ""
    finally:
        with _lock:
            _running.pop(job, None)
    secs = time.monotonic() - t0
    print(f"[DeepThink] ✅ job {job} done in {secs:.0f}s")
    if player is not None:
        if detail:
            try:
                player.show_content(f"🧠 {task[:40]}", f"{spoken}\n\n{detail}")
            except Exception:
                pass
        deliver = getattr(player, "request_say_idle", None) or getattr(player, "request_say", None)
        if callable(deliver):
            deliver(
                f"[SECOND BRAIN RESULT] The background work you started for the user is done.\n"
                f"Task: {task}\nAnswer: {spoken}\n"
                f"Tell the user this answer now, naturally, in their language, without "
                f"repeating the question" + (" and mention the details are on screen." if detail else ".")
                + " Do not call deep_think again for this.")


def run(parameters: dict, player=None, session_memory=None) -> str:
    global _next_id
    p = parameters or {}
    task = str(p.get("task", "")).strip()
    if str(p.get("action", "")).lower() == "status":
        with _lock:
            jobs = list(_running.values())
        return ("Working on: " + "; ".join(jobs)) if jobs else "Nothing is running in the background."
    if not task:
        return "Say what the second brain should work on (task)."

    context = str(p.get("context", "")).strip()
    try:                                     # the last turns, so "that one" makes sense
        from memory import conversation
        recent = conversation.prompt_block("assistant")
        if recent:
            context = (context + "\n\n" + recent[-2500:]).strip()
    except Exception:
        pass
    with _lock:
        _next_id += 1
        job = _next_id
        _running[job] = task
    threading.Thread(target=_work, args=(job, task, context, player), daemon=True,
                     name=f"deep-think-{job}").start()
    try:
        if player is not None:
            player.write_log(f"SYS: 🧠 Segundo cérebro trabalhando: {task[:80]}")
    except Exception:
        pass
    return ("[STARTED IN BACKGROUND] The second brain is working on it; it usually takes "
            "10–60 seconds. Tell the user in ONE short sentence, in their language, that "
            "you're on it and will tell them shortly. Do NOT answer the question yourself "
            "and do not guess. You may keep talking about other things meanwhile; the "
            "answer will arrive as a [SECOND BRAIN RESULT] message.")


PLUGIN = {
    "name": "deep_think",
    "description": (
        "Second brain for HARD requests — hand off anything that needs several steps: "
        "research across sources, comparing products or options, planning (trips, "
        "schedules, budgets), calculations with current data, long writing, analysing "
        "a situation. It runs in the background (10–60 s) while you keep talking; the "
        "answer comes back later by itself. Do NOT use it for simple facts, quick "
        "lookups, weather, or device control — use the direct tools for those. "
        "action 'status' lists what it is working on."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "task": {"type": "STRING",
                     "description": "The full request, self-contained, in the user's words, "
                                    "with every constraint they gave (budget, dates, city…)."},
            "context": {"type": "STRING",
                        "description": "Anything from the conversation it needs (optional)."},
            "action": {"type": "STRING", "description": "'status' to list running jobs (optional)"},
        },
        "required": [],
    },
}


def _check(values: dict):
    from core import openai_brain
    return openai_brain.check(values)


PLUGIN_SETTINGS = {
    "namespace": "brains",
    "title": "Segundo cérebro — GPT + Gemini",
    "fields": [
        {"key": "openai_api_key", "label": "OpenAI API key (platform.openai.com → API keys)",
         "type": "password"},
        {"key": "provider", "label": "Quem pensa primeiro", "type": "choice",
         "options": ["auto", "openai", "gemini"]},
        {"key": "openai_model", "label": "Modelo GPT (vazio = o melhor disponível)", "type": "text",
         "placeholder": "automático"},
    ],
    "action": {"label": "TESTAR", "run": _check},
}
