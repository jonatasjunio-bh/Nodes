"""
core/deep_brain.py — the assistant's second brain.

The live voice model is quick but works in a single breath: it can't research
across several sources, weigh them, plan or write at length without leaving
the user in silence while it does. So that work is handed here, run in the
background, and the answer is brought back into the conversation when it is
ready (plugins/deep_think.py does the handing over).

This brain is a text Gemini model with two tools of its own — a web search
(DuckDuckGo, the same engine web_search uses) and a page reader — called in a
loop by the SDK until it has an answer. Gemini's built-in Google Search tool is
not used: on this key its quota is the first to run out, and a search engine
we call ourselves has no such limit.

Models are tried in order and one that is out of quota or down is skipped for
a while, through the same cooldown bookkeeping as core/gemini.py.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor

from core import gemini

# Measured on this key: 3.6-flash answers (≈3.6 s), 3.5-flash and the Pro
# models are usually out of free quota, flash-latest is slower but up. The
# lite models at the bottom still reason well enough with tools.
LADDER = ("gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite",
          "gemini-3.1-flash-lite", "gemini-3.1-pro-preview", "gemini-flash-latest")
MAX_TOOL_CALLS = 8
REQUEST_TIMEOUT_MS = 35_000     # a model that hangs (504 after 90 s, seen) is dropped sooner
BUDGET_S = 80                   # then answer with what has been gathered
PAGE_CHARS = 6000

_SYSTEM = """You are the research and reasoning half of a personal voice assistant.
The other half is talking with the user right now; you work in the background
and your answer will be SPOKEN to the user by it, so:

- Use the tools: search the web for anything current or factual (prices,
  news, schedules, products, places, people) and read the most relevant pages.
  Several searches from different angles beat one — ask for them together in
  one step (several search_web calls at once). Never invent figures.
- Work the problem fully: compare, calculate, plan, decide — then answer.
- Answer in the user's language (Brazilian Portuguese unless the task is in
  another language).

Reply in exactly this format:
FALA: <the answer to be spoken: at most 4 short sentences, natural speech, no
lists, no markdown, no URLs; lead with the conclusion>
DETALHES: <optional — the supporting detail for the screen: short bullet points,
figures, and the source sites>"""


# ── the brain's own tools (plain functions: the SDK calls them) ──────────────

def search_web(query: str, news: bool = False) -> str:
    """Search the web (DuckDuckGo). Set news=True for recent news articles.
    Returns titles, snippets and URLs of the top results."""
    from actions import web_search as ws
    results = ws._ddg_news(query, 8) if news else ws._ddg_search(query, 8)
    print(f"[DeepBrain] 🔎 {'news: ' if news else ''}{query} → {len(results)} results")
    return ws._format_ddg(query, results)


def read_page(url: str) -> str:
    """Fetch a web page and return its readable text (truncated)."""
    import requests
    from bs4 import BeautifulSoup
    print(f"[DeepBrain] 📄 {url[:90]}")
    try:
        r = requests.get(url, timeout=12, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"})
        r.raise_for_status()
    except Exception as e:
        return f"Could not open the page: {e}"
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    text = re.sub(r"\n\s*\n+", "\n", soup.get_text("\n")).strip()
    return text[:PAGE_CHARS] or "The page had no readable text."


# ── the loop ─────────────────────────────────────────────────────────────────

_SYSTEM_GPT = _SYSTEM.replace(
    "- Use the tools: search the web for anything current or factual (prices,\n"
    "  news, schedules, products, places, people) and read the most relevant pages.",
    "- Use web search for anything current or factual (prices, news, schedules,\n"
    "  products, places, people), and open the most relevant sources.")


def think(task: str, context: str = "") -> tuple[str, str]:
    """Work on `task` to the end. Returns (spoken answer, detail for the screen).
    GPT first when it is configured (core/openai_brain.py), Gemini otherwise or
    when GPT fails. Never raises: on total failure the spoken answer says so."""
    try:
        from core import openai_brain
        if openai_brain.enabled():
            prompt = task.strip() + (f"\n\n[Context from the conversation]\n{context.strip()}"
                                     if context.strip() else "")
            deep, _fast = openai_brain.pick_models()
            t0 = time.perf_counter()
            out = openai_brain.respond(_SYSTEM_GPT, prompt, deep, web=True, effort="low",
                                       timeout=BUDGET_S + 40)
            print(f"[DeepBrain] 🧠 {deep} (OpenAI) answered in {time.perf_counter() - t0:.1f}s")
            return _split(out)
    except Exception as e:
        print(f"[DeepBrain] OpenAI unavailable ({str(e)[:120]}) — using Gemini")
    return _think_gemini(task, context)


def _think_gemini(task: str, context: str = "") -> tuple[str, str]:
    from google.genai import types

    key = gemini.api_key()
    if not key:
        return "Não tenho uma chave do Gemini configurada para pensar nisso.", ""
    prompt = task.strip()
    if context.strip():
        prompt = f"{prompt}\n\n[Context from the conversation]\n{context.strip()}"
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]

    # Our own loop rather than the SDK's automatic one: left to itself the
    # model happily answered "today's petrol price" from memory. The first step
    # is forced to be a search; after that it decides, up to MAX_TOOL_CALLS.
    def cfg(force_tool: bool):
        return types.GenerateContentConfig(
            system_instruction=_SYSTEM,
            tools=[types.Tool(function_declarations=_DECLS)],
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
                mode="ANY" if force_tool else "AUTO")),
            temperature=0.4,
        )

    cl = gemini.client(timeout_ms=REQUEST_TIMEOUT_MS, key=key)
    calls_made = 0
    t0 = time.perf_counter()
    while True:
        # The free tier allows only a few requests a minute PER MODEL, and a
        # research loop makes several in a row: a 429 benches that model for
        # exactly the delay Google asks for, and the next one carries on.
        model = next((m for m in LADDER if not gemini._cooling(m)), None)
        if model is None or time.perf_counter() - t0 > BUDGET_S:
            break
        try:
            last = calls_made >= MAX_TOOL_CALLS
            resp = cl.models.generate_content(
                model=model, contents=contents, config=cfg(force_tool=calls_made == 0))
            fcs = [] if last else list(resp.function_calls or [])
            if not fcs:
                out = (resp.text or "").strip()
                if not out:
                    raise RuntimeError("empty answer")
                print(f"[DeepBrain] 🧠 {model}: {calls_made} tool calls, "
                      f"{time.perf_counter() - t0:.1f}s")
                return _split(out)
            contents.append(resp.candidates[0].content)
            def _run(fc):
                fn = _TOOLS.get(fc.name)
                try:
                    return fn(**dict(fc.args or {})) if fn else f"unknown tool {fc.name}"
                except Exception as e:
                    return f"tool failed: {e}"
            # Searches asked for together run together: one slow search engine
            # no longer holds up the others.
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(_run, fcs))
            calls_made += len(fcs)
            parts = [types.Part.from_function_response(name=fc.name, response={"result": r})
                     for fc, r in zip(fcs, results)]
            contents.append(types.Content(role="user", parts=parts))
        except Exception as e:
            msg = str(e)
            if gemini.is_quota_error(msg):
                m = re.search(r"retryDelay'?:\s*'(\d+(?:\.\d+)?)s", msg)
                wait = float(m.group(1)) + 1 if m else gemini._COOLDOWN_SECONDS
                gemini._cool(model, min(wait, gemini._COOLDOWN_SECONDS))
                print(f"[DeepBrain] {model}: rate limit — resting {min(wait, 300):.0f}s")
            else:
                if gemini.is_gone_error(msg):
                    gemini._cool(model, gemini._GONE_SECONDS)
                else:                    # 504 / overloaded / anything odd: not now
                    gemini._cool(model, gemini._UNAVAILABLE_SECONDS)
                print(f"[DeepBrain] {model}: {type(e).__name__}: {msg[:120]}")
    if calls_made:
        # Out of time or models mid-way: one last plain answer from whatever
        # was gathered, on the fastest model that will take it.
        try:
            contents.append(types.Content(role="user", parts=[types.Part.from_text(
                text="Time is up: answer now with what you have, in the required format.")]))
            for model in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest"):
                if gemini._cooling(model):
                    continue
                resp = cl.models.generate_content(model=model, contents=contents,
                                                  config=types.GenerateContentConfig(
                                                      system_instruction=_SYSTEM, temperature=0.4,
                                                      tools=[types.Tool(function_declarations=_DECLS)],
                                                      tool_config=types.ToolConfig(
                                                          function_calling_config=types.FunctionCallingConfig(
                                                              mode="NONE"))))
                if (resp.text or "").strip():
                    print(f"[DeepBrain] 🧠 {model}: closing answer after {calls_made} tool calls")
                    return _split(resp.text.strip())
        except Exception as e:
            print(f"[DeepBrain] closing answer failed: {e}")
    return ("Não consegui terminar essa análise agora — os modelos de raciocínio do Gemini "
            "estão sem cota ou fora do ar. Tente de novo daqui a pouco."), ""


_TOOLS = {"search_web": search_web, "read_page": read_page}
_DECLS = [
    {"name": "search_web",
     "description": "Search the web (DuckDuckGo). Returns titles, snippets and URLs of the "
                    "top results. Set news=true for recent news articles.",
     "parameters": {"type": "OBJECT", "properties": {
         "query": {"type": "STRING", "description": "What to search for"},
         "news": {"type": "BOOLEAN", "description": "Recent news instead of general results"}},
         "required": ["query"]}},
    {"name": "read_page",
     "description": "Open a web page (a URL from the search results) and return its text.",
     "parameters": {"type": "OBJECT", "properties": {
         "url": {"type": "STRING", "description": "The page's full URL"}},
         "required": ["url"]}},
]


def _split(out: str) -> tuple[str, str]:
    m = re.search(r"FALA:\s*(.*?)(?:\n\s*DETALHES:\s*(.*))?\Z", out, re.S | re.I)
    if not m:
        return out[:700], out
    return m.group(1).strip(), (m.group(2) or "").strip()
