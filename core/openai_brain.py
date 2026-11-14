"""
core/openai_brain.py — GPT as the assistant's second brain.

Who does what (the voice stays on Gemini Live — it is built for real-time
speech; this is the brain for the work done in the background):

    deep research / reasoning   GPT, flagship model, with OpenAI's built-in web
                                search — then Gemini if GPT is unavailable
    hologram drawings (SVG)     GPT "mini" model (fast) — then Gemini

Configured in ⚙ → Central de Módulos → Segundo Cérebro (namespace "brains"):
the OpenAI API key, the provider order (auto = GPT first) and, optionally, a
model to force. With no key everything keeps running on Gemini as before.

The REST API is called directly (requests), so this works whatever version of
the openai package is installed — or none.
"""

from __future__ import annotations

import re
import time

import requests

NAMESPACE = "brains"
API = "https://api.openai.com/v1"

_models_cache: tuple[float, list[str]] = (0.0, [])


def _cfg() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        return get_plugin_config(NAMESPACE) or {}
    except Exception:
        return {}


def api_key() -> str:
    return str(_cfg().get("openai_api_key") or "").strip()


def provider() -> str:
    p = str(_cfg().get("provider") or "auto").strip().lower()
    return p if p in ("auto", "openai", "gemini") else "auto"


def enabled() -> bool:
    """GPT should be tried (first) for background work."""
    return bool(api_key()) and provider() != "gemini"


def _headers() -> dict:
    return {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}


# ── which models this key can use ────────────────────────────────────────────

def list_models(force: bool = False) -> list[str]:
    global _models_cache
    at, ids = _models_cache
    if ids and not force and time.monotonic() - at < 3600:
        return ids
    r = requests.get(f"{API}/models", headers=_headers(), timeout=15)
    r.raise_for_status()
    ids = sorted(m.get("id", "") for m in r.json().get("data", []))
    _models_cache = (time.monotonic(), ids)
    return ids


_SKIP = ("realtime", "audio", "transcribe", "tts", "image", "search", "codex", "nano",
         "instruct", "embedding", "moderation", "chat-latest", "-pro", "oss", "preview")


def _version(mid: str) -> tuple | None:
    """gpt-5.1 → (5, 1); gpt-5-mini → (5, 0); None for anything not a plain
    gpt-N[.M][-mini] chat model (dated snapshots, audio, codex… are skipped)."""
    if any(s in mid for s in _SKIP):
        return None
    m = re.fullmatch(r"gpt-(\d+)(?:\.(\d+))?(-mini)?", mid)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0)


def pick_models() -> tuple[str, str]:
    """(deep model, fast model) — the newest flagship and the newest mini this
    key offers; a model forced in the settings wins for 'deep'."""
    forced = str(_cfg().get("openai_model") or "").strip()
    ids = list_models()
    full = sorted((v, i) for i in ids if (v := _version(i)) and not i.endswith("-mini"))
    mini = sorted((v, i) for i in ids if (v := _version(i)) and i.endswith("-mini"))
    deep = forced or (full[-1][1] if full else (mini[-1][1] if mini else "gpt-4.1"))
    fast = mini[-1][1] if mini else deep
    return deep, fast


# ── one call ─────────────────────────────────────────────────────────────────

def respond(instructions: str, prompt: str, model: str, web: bool = False,
            effort: str = "low", timeout: float = 120.0,
            images: list[tuple[bytes, str]] | None = None) -> str:
    """One Responses-API call; returns the output text. Tries the variants
    older/newer models accept (reasoning or not, web_search tool names).
    images: [(bytes, mime)] sent along with the prompt (vision)."""
    if images:
        import base64
        content = [{"type": "input_text", "text": prompt}] + [
            {"type": "input_image",
             "image_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}
            for data, mime in images]
        prompt = [{"role": "user", "content": content}]
    tool_variants = (["web_search", "web_search_preview", None] if web else [None])
    reasoning_variants = [effort, None]
    last = ""
    for tool in tool_variants:
        for eff in reasoning_variants:
            body = {"model": model, "instructions": instructions, "input": prompt}
            if tool:
                body["tools"] = [{"type": tool}]
            if eff:
                body["reasoning"] = {"effort": eff}
            r = requests.post(f"{API}/responses", headers=_headers(), json=body, timeout=timeout)
            if r.status_code == 200:
                text = _output_text(r.json())
                if text:
                    return text
                last = "empty answer"
                continue
            err = r.text[:300]
            last = f"{r.status_code}: {err}"
            if r.status_code in (401, 403, 429) or r.status_code >= 500:
                raise RuntimeError(last)              # key, quota or outage: next brain
            low = err.lower()
            if eff and "reasoning" in low:
                continue                               # this model takes no reasoning param
            if tool and ("tool" in low or tool in low):
                break                                  # try the next tool name
            raise RuntimeError(last)
    raise RuntimeError(last)


def _output_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"].strip()
    parts = []
    for item in data.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    parts.append(c["text"])
    return "\n".join(parts).strip()


def check(values: dict | None = None) -> tuple[bool, str]:
    """⚙ TESTAR: the key works, which models were picked, a one-line answer."""
    if values:
        try:
            from memory.config_manager import save_plugin_config
            save_plugin_config(NAMESPACE, values)
        except Exception:
            pass
    if not api_key():
        return False, "Cole a API key da OpenAI (platform.openai.com → API keys)."
    try:
        list_models(force=True)
        deep, fast = pick_models()
        t = time.perf_counter()
        out = respond("Answer with one word.", "Say: ok", fast, timeout=40)
        return True, (f"Conectado. Pesquisa/raciocínio: {deep} · desenhos: {fast} "
                      f"(teste {time.perf_counter() - t:.1f}s: '{out[:20]}').")
    except Exception as e:
        msg = str(e)
        if "401" in msg:
            return False, "A OpenAI recusou a chave (401) — confira se foi copiada inteira."
        if "429" in msg:
            return False, "Sem cota na OpenAI (429) — a conta precisa de créditos na API."
        return False, f"Não consegui falar com a OpenAI: {msg[:160]}"


# ── images (realistic holograms) ─────────────────────────────────────────────

def pick_image_model() -> str | None:
    """The newest plain gpt-image-N this key offers (gpt-image-2 > 1.5 > 1),
    else chatgpt-image-latest; None when the key has no image model."""
    ids = list_models()
    plain = []
    for i in ids:
        m = re.fullmatch(r"gpt-image-(\d+)(?:\.(\d+))?", i)
        if m:
            plain.append(((int(m.group(1)), int(m.group(2) or 0)), i))
    if plain:
        return sorted(plain)[-1][1]
    return "chatgpt-image-latest" if "chatgpt-image-latest" in ids else None


def generate_image(prompt: str, quality: str = "low", reference: tuple[bytes, str] | None = None,
                   timeout: float = 180.0) -> bytes:
    """A 1024×1024 PNG. With `reference` (bytes, mime) the picture is an edit
    of that image (same subject, new look). Raises RuntimeError on failure."""
    import base64
    model = pick_image_model()
    if not model:
        raise RuntimeError("this OpenAI key has no image model")
    if reference:
        data, mime = reference
        r = requests.post(f"{API}/images/edits", headers={"Authorization": f"Bearer {api_key()}"},
                          timeout=timeout,
                          data={"model": model, "prompt": prompt, "size": "1024x1024", "quality": quality},
                          files={"image": ("reference" + (".png" if "png" in mime else ".jpg"), data, mime)})
    else:
        r = requests.post(f"{API}/images/generations", headers=_headers(), timeout=timeout,
                          json={"model": model, "prompt": prompt, "size": "1024x1024",
                                "quality": quality, "n": 1})
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:200]}")
    d = (r.json().get("data") or [{}])[0]
    if d.get("b64_json"):
        return base64.b64decode(d["b64_json"])
    if d.get("url"):
        return requests.get(d["url"], timeout=60).content
    raise RuntimeError("the image API returned no picture")
